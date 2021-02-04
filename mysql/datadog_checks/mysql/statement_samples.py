import json
import os
import re
import time
from concurrent.futures.thread import ThreadPoolExecutor
from contextlib import closing

import pymysql
from cachetools import TTLCache

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent

from datadog_checks.base import is_affirmative
from datadog_checks.base.log import get_check_logger
from datadog_checks.base.utils.db.sql import compute_exec_plan_signature, compute_sql_signature
from datadog_checks.base.utils.db.utils import ConstantRateLimiter, resolve_db_host
from datadog_checks.base.utils.db.statement_samples import statement_samples_client

VALID_EXPLAIN_STATEMENTS = frozenset({'select', 'table', 'delete', 'insert', 'replace', 'update'})

# unless a specific table is configured, we try all of the events_statements tables in descending order of
# preference
EVENTS_STATEMENTS_PREFERRED_TABLES = [
    'events_statements_history_long',
    'events_statements_current',
    # events_statements_history is the lowest in preference because it keeps the history only as long as the thread
    # exists, which means if an application uses only short-lived connections that execute a single query then we
    # won't be able to catch any samples of it. By querying events_statements_current we at least guarantee we'll
    # be able to catch queries from short-lived connections.
    'events_statements_history'
]

# default sampling settings for events_statements_* tables
# rate limit is in samples/second
# {table -> rate-limit}
DEFAULT_EVENTS_STATEMENTS_COLLECTIONS_PER_SECOND = {
    'events_statements_history_long': 1 / 10,
    'events_statements_history': 1 / 10,
    'events_statements_current': 1,
}

# columns from events_statements_summary tables which correspond to attributes common to all databases and are
# therefore stored under other standard keys
EVENTS_STATEMENTS_SAMPLE_EXCLUDE_KEYS = {
    # gets obfuscated
    'sql_text',
    # stored as "instance"
    'current_schema',
    # used for signature
    'digest_text',
    'timer_end_time_s',
    'max_timer_wait_ns',
    'timer_start',
    # included as network.client.ip
    'processlist_host'
}

EVENTS_STATEMENTS_QUERY = re.sub(r'\s+', ' ', """
    SELECT current_schema,
           sql_text,
           IFNULL(digest_text, sql_text) AS digest_text,
           timer_start,
           UNIX_TIMESTAMP()-(select VARIABLE_VALUE from performance_schema.global_status
                    where VARIABLE_NAME='UPTIME')+timer_end*1e-12 as timer_end_time_s,
           timer_wait / 1000 AS timer_wait_ns,
           lock_time / 1000 AS lock_time_ns,
           rows_affected,
           rows_sent,
           rows_examined,
           select_full_join,
           select_full_range_join,
           select_range,
           select_range_check,
           select_scan,
           sort_merge_passes,
           sort_range,
           sort_rows,
           sort_scan,
           no_index_used,
           no_good_index_used,
           processlist_user,
           processlist_host,
           processlist_db
        FROM performance_schema.{events_statements_table} as E
        LEFT JOIN performance_schema.threads as T
        ON E.thread_id = T.thread_id
        WHERE sql_text IS NOT NULL
            AND event_name like %s
            AND (digest_text is NULL OR digest_text NOT LIKE %s)
            AND timer_start > %s
    ORDER BY timer_wait DESC
    LIMIT %s
""")

PYMYSQL_NON_RETRYABLE_ERRORS = frozenset(
    {
        1044,  # access denied on database
        1046,  # no permission on statement
        1049,  # unknown database
        1305,  # procedure does not exist
        1370,  # no execute on procedure
    }
)


class MySQLStatementSamples(object):
    executor = ThreadPoolExecutor()

    """
    Collects statement samples and execution plans. Where defined, the user will attempt
    to use the stored procedure `explain_statement` which allows collection of statement samples
    using the permissions of the procedure definer.
    """

    def __init__(self, check, config, connection_args):
        self._check = check
        self._connection_args = connection_args
        # checkpoint at zero so we pull the whole history table on the first run
        self._checkpoint = 0
        self._log = get_check_logger()
        self._last_check_run = 0
        self._db = None
        self._tags = None
        self._tags_str = None
        self._service = "mysql"
        self._collection_loop_future = None
        self._rate_limiter = ConstantRateLimiter(1)
        self._config = config
        self._db_hostname = resolve_db_host(self._config.host)
        self._enabled = is_affirmative(self._config.statement_samples_config.get('enabled', False))
        self._debug = is_affirmative(self._config.statement_samples_config.get('debug', False))
        self._run_sync = is_affirmative(self._config.statement_samples_config.get('run_sync', False))
        self._auto_enable_events_statements_consumers = is_affirmative(
            self._config.statement_samples_config.get('auto_enable_events_statements_consumers', False))
        self._collections_per_second = self._config.statement_samples_config.get('collections_per_second', -1)
        self._events_statements_row_limit = self._config.statement_samples_config.get('events_statements_row_limit',
                                                                                      5000)
        self._explain_procedure = self._config.statement_samples_config.get('explain_procedure', 'explain_statement')
        self._fully_qualified_explain_procedure = self._config.statement_samples_config.get(
            'fully_qualified_explain_procedure',
            'datadog.explain_statement'
        )
        self._preferred_events_statements_tables = EVENTS_STATEMENTS_PREFERRED_TABLES
        events_statements_table = self._config.statement_samples_config.get('events_statements_table', None)
        if events_statements_table:
            if events_statements_table in DEFAULT_EVENTS_STATEMENTS_COLLECTIONS_PER_SECOND:
                self._log.info("using configured events_statements_table: %s", events_statements_table)
                self._preferred_events_statements_tables = [events_statements_table]
            else:
                self._log.warning(
                    "invalid events_statements_table: %s. must be one of %s",
                    events_statements_table,
                    ', '.join(DEFAULT_EVENTS_STATEMENTS_COLLECTIONS_PER_SECOND.keys()),
                )

        self._collection_strategy_cache = TTLCache(
            maxsize=self._config.statement_samples_config.get('collection_strategy_cache_maxsize', 1000),
            ttl=self._config.statement_samples_config.get('collection_strategy_cache_ttl', 300)
        )

        # explained_statements_cache: limit how often we try to re-explain the same query
        self._explained_statements_cache = TTLCache(
            maxsize=self._config.statement_samples_config.get('explained_statements_cache_maxsize', 5000),
            ttl=60 * 60 / self._config.statement_samples_config.get('explained_statements_per_hour_per_query', 60)
        )

        # seen_samples_cache: limit the ingestion rate per (query_signature, plan_signature)
        self._seen_samples_cache = TTLCache(
            # assuming ~60 bytes per entry (query & plan signature, key hash, 4 pointers (ordered dict), expiry time)
            # total size: 10k * 60 = 0.6 Mb
            maxsize=self._config.statement_samples_config.get('seen_samples_cache_maxsize', 10000),
            ttl=60 * 60 / self._config.statement_samples_config.get('samples_per_hour_per_query', 15)
        )

        self._explain_strategies = {
            'PROCEDURE': self._run_explain_procedure,
            'FQ_PROCEDURE': self._run_fully_qualified_explain_procedure,
            'STATEMENT': self._run_explain,
        }

        self._preferred_explain_strategies = ['PROCEDURE', 'FQ_PROCEDURE', 'STATEMENT']

    def run_sampler(self, tags):
        """
        start the sampler thread if not already running & update tag metadata
        :param tags:
        :return:
        """
        if not self._enabled:
            return
        self._tags = tags
        self._tags_str = ','.join(tags)
        for t in self._tags:
            if t.startswith('service:'):
                self._service = t[len('service:'):]
        self._last_check_run = time.time()
        if self._run_sync or is_affirmative(os.environ.get('DBM_STATEMENT_SAMPLER_RUN_SYNC', "false")):
            self._log.debug("running statement sampler synchronously")
            self._collect_statement_samples()
        elif self._collection_loop_future is None or not self._collection_loop_future.running():
            self._log.info("starting mysql statement sampler")
            self._collection_loop_future = MySQLStatementSamples.executor.submit(self.collection_loop)
        else:
            self._log.debug("mysql statement sampler already running")

    def _get_db_connection(self):
        """
        lazy reconnect db
        pymysql connections are not thread safe so we can't reuse the same connection from the main check
        :return:
        """
        if not self._db:
            self._db = pymysql.connect(**self._connection_args)
        return self._db

    def collection_loop(self):
        try:
            self._log.info("started mysql statement sampler collection loop")
            while True:
                if time.time() - self._last_check_run > self._config.min_collection_interval * 2:
                    self._log.info("stopping mysql statement sampler collection loop due to check inactivity")
                    self._check.count("dd.mysql.statement_samples.collection_loop_inactive_stop", 1, tags=self._tags)
                    break
                self._collect_statement_samples()
        except Exception as e:
            self._log.exception("mysql statement sampler collection loop failure")
            self._check.count("dd.mysql.statement_samples.error", 1,
                              tags=self._tags + ["error:collection-loop-failure-{}".format(type(e))])

    def _get_new_events_statements(self, events_statements_table, row_limit):
        # Select the most recent events with a bias towards events which have higher wait times
        start = time.time()
        query = EVENTS_STATEMENTS_QUERY.format(events_statements_table=events_statements_table)
        with closing(self._get_db_connection().cursor(pymysql.cursors.DictCursor)) as cursor:
            params = ('statement/%', 'EXPLAIN %', self._checkpoint, row_limit)
            self._log.debug("running query: " + query, *params)
            cursor.execute(query, params)
            rows = cursor.fetchall()
            if not rows:
                self._log.debug("no statements found in performance_schema.%s", events_statements_table)
                return rows
            self._checkpoint = max(r['timer_start'] for r in rows)
            cursor.execute('SET @@SESSION.sql_notes = 0')
            tags = ["table:%s".format(events_statements_table)] + self._tags
            self._check.histogram("dd.mysql.get_new_events_statements.time", (time.time() - start) * 1000, tags=tags)
            self._check.histogram("dd.mysql.get_new_events_statements.rows", len(rows), tags=tags)
            return rows

    def _filter_valid_statement_rows(self, rows):
        num_sent = 0
        num_truncated = 0

        for row in rows:
            if not row or not all(row):
                self._log.debug('Row was unexpectedly truncated or events_statements_history_long table is not enabled')
                continue

            sql_text = row['sql_text']
            if not sql_text:
                continue

            # The SQL_TEXT column will store 1024 chars by default. Plans cannot be captured on truncated
            # queries, so the `performance_schema_max_sql_text_length` variable must be raised.
            if sql_text[-3:] == '...':
                num_truncated += 1
                continue

            yield row
            num_sent += 1

        if num_truncated > 0:
            self._log.warning(
                'Unable to collect %d/%d statement samples due to truncated SQL text. Consider raising '
                '`performance_schema_max_sql_text_length` to capture these queries.',
                num_truncated,
                num_truncated + num_sent,
            )
            self._check.count("dd.mysql.statement_samples.error", 1, tags=self._tags + ["error:truncated-sql-text"])

    def _collect_plans_for_statements(self, rows):
        for row in self._filter_valid_statement_rows(rows):
            # Plans have several important signatures to tag events with:
            # - `plan_signature` - hash computed from the normalized JSON plan to group identical plan trees
            # - `resource_hash` - hash computed off the raw sql text to match apm resources
            # - `query_signature` - hash computed from the digest text to match query metrics

            try:
                obfuscated_statement = datadog_agent.obfuscate_sql(row['sql_text'])
            except Exception:
                self._log.debug("failed to obfuscate statement: %s", row['sql_text'])
                self._check.count("dd.mysql.statement_samples.error", 1, tags=self._tags + ["error:sql-obfuscate"])
                continue

            query_signature = compute_sql_signature(datadog_agent.obfuscate_sql(row['digest_text']))
            apm_resource_hash = compute_sql_signature(obfuscated_statement)
            if query_signature in self._explained_statements_cache:
                continue
            self._explained_statements_cache[query_signature] = True

            normalized_plan, obfuscated_plan, plan_signature, plan_cost = None, None, None, None
            plan = self._explain_statement_safe(row['sql_text'], row['current_schema'])
            if plan:
                normalized_plan = datadog_agent.obfuscate_sql_exec_plan(plan, normalize=True) if plan else None
                obfuscated_plan = datadog_agent.obfuscate_sql_exec_plan(plan)
                plan_signature = compute_exec_plan_signature(normalized_plan)
                plan_cost = self._parse_execution_plan_cost(plan)

            statement_plan_sig = (query_signature, plan_signature)
            if statement_plan_sig not in self._seen_samples_cache:
                self._seen_samples_cache[statement_plan_sig] = True
                yield {
                    "timestamp": row["timer_end_time_s"] * 1000,
                    "host": self._db_hostname,
                    "service": self._service,
                    "ddsource": "mysql",
                    "ddtags": self._tags_str,
                    "duration": row['timer_wait_ns'],
                    "network": {
                        "client": {
                            "ip": row.get('processlist_host', None),
                        }
                    },
                    "db": {
                        "instance": row['current_schema'],
                        "plan": {
                            "definition": obfuscated_plan,
                            "cost": plan_cost,
                            "signature": plan_signature
                        },
                        "query_signature": query_signature,
                        "resource_hash": apm_resource_hash,
                        "statement": obfuscated_statement
                    },
                    'mysql': {k: v for k, v in row.items() if k not in EVENTS_STATEMENTS_SAMPLE_EXCLUDE_KEYS},
                }

    def _get_enabled_performance_schema_consumers(self):
        """
        Returns the list of available performance schema consumers
        I.e. (events_statements_current, events_statements_history)
        :return:
        """
        with closing(self._get_db_connection().cursor()) as cursor:
            cursor.execute("SELECT name from performance_schema.setup_consumers WHERE enabled = 'YES'")
            enabled_consumers = set([r[0] for r in cursor.fetchall()])
            self._log.debug("loaded enabled consumers: %s", enabled_consumers)
            return enabled_consumers

    def _performance_schema_enable_consumer(self, name):
        query = """UPDATE performance_schema.setup_consumers SET enabled = 'YES' WHERE name = %s"""
        with closing(self._get_db_connection().cursor()) as cursor:
            try:
                cursor.execute(query, name)
                self._log.debug('successfully enabled performance_schema consumer %s', name)
                return True
            except pymysql.err.DatabaseError as e:
                if e.args[0] == 1290:
                    # --read-only mode failure is expected so log at debug level
                    self._log.debug('failed to enable performance_schema consumer %s: %s', name, e)
                    return False
                self._log.debug('failed to enable performance_schema consumer %s: %s', name, e)
        return False

    def _get_sample_collection_strategy(self):
        """
        Decides on the plan collection strategy:
        - which events_statement_history-* table are we using
        - how long should the rate and time limits be
        :return: (table, rate_limit)
        """
        cached_strategy = self._collection_strategy_cache.get("plan_collection_strategy")
        if cached_strategy:
            self._log.debug("using cached plan_collection_strategy: %s", cached_strategy)
            return cached_strategy

        enabled_consumers = self._get_enabled_performance_schema_consumers()

        rate_limit = self._collections_per_second
        events_statements_table = None
        for table in self._preferred_events_statements_tables:
            if table not in enabled_consumers:
                if not self._auto_enable_events_statements_consumers:
                    self._log.debug("performance_schema consumer for table %s not enabled", table)
                    continue
                if not self._performance_schema_enable_consumer(table):
                    continue
                self._log.debug("successfully enabled performance_schema consumer")
            rows = self._get_new_events_statements(table, 1)
            if not rows:
                self._log.debug("no statements found in %s", table)
                continue
            if rate_limit < 0:
                rate_limit = DEFAULT_EVENTS_STATEMENTS_COLLECTIONS_PER_SECOND[table]
            events_statements_table = table
            break

        if not events_statements_table:
            self._log.info(
                "no valid performance_schema.events_statements table found. cannot collect statement samples.")
            return None, None

        # cache only successful strategies
        # should be short enough that we'll reflect updates relatively quickly
        # i.e., an aurora replica becomes a master (or vice versa).
        strategy = (events_statements_table, rate_limit)
        self._log.debug("chosen plan collection strategy: events_statements_table=%s, rate_limit=%s",
                        events_statements_table, rate_limit)
        self._collection_strategy_cache["plan_collection_strategy"] = strategy
        return strategy

    def _collect_statement_samples(self):
        self._rate_limiter.sleep()

        events_statements_table, rate_limit = self._get_sample_collection_strategy()
        if not events_statements_table:
            return
        if self._rate_limiter.rate_limit_s != rate_limit:
            self._rate_limiter = ConstantRateLimiter(rate_limit)

        start_time = time.time()
        tags = self._tags + ["events_statements_table:{}".format(events_statements_table)]
        rows = self._get_new_events_statements(events_statements_table, self._events_statements_row_limit)
        events = self._collect_plans_for_statements(rows)
        submitted_count = statement_samples_client.submit_events(events)
        self._check.histogram("dd.mysql.collect_statement_samples.time", (time.time() - start_time) * 1000, tags=tags)
        self._check.count("dd.mysql.collect_statement_samples.events_submitted.count",
                          submitted_count, tags=tags)
        self._check.gauge("dd.mysql.collect_statement_samples.seen_samples_cache.len", len(self._seen_samples_cache),
                          tags=tags)
        self._check.gauge("dd.mysql.collect_statement_samples.explained_statements_cache.len",
                          len(self._explained_statements_cache), tags=tags)

    def _explain_statement_safe(self, sql_text, schema):
        start_time = time.time()
        with closing(self._get_db_connection().cursor()) as cursor:
            try:
                plan = self._explain_statement(cursor, sql_text, schema)
                self._check.histogram("dd.mysql.run_explain.time", (time.time() - start_time) * 1000, tags=self._tags)
                return plan
            except Exception as e:
                self._check.count("dd.mysql.statement_samples.error", 1,
                                  tags=self._tags + ["error:explain-{}".format(type(e))])
                self._log.exception("failed to run explain on query %s", sql_text)

    def _explain_statement(self, cursor, statement, schema):
        """
        Tries the available methods used to explain a statement for the given schema. If a non-retryable
        error occurs (such as a permissions error), then statements executed under the schema will be
        disallowed in future attempts.
        """
        # Obfuscate the statement for logging
        obfuscated_statement = datadog_agent.obfuscate_sql(statement)
        strategy_cache_key = 'explain_strategy:%s' % schema
        explain_strategy_error = 'ERROR'
        tags = self._tags + ["schema:{}".format(schema)]

        if not self._can_explain(statement):
            self._log.debug('Skipping statement which cannot be explained: %s', obfuscated_statement)
            return None

        if self._collection_strategy_cache.get(strategy_cache_key) == explain_strategy_error:
            self._log.debug('Skipping statement due to cached collection failure: %s', obfuscated_statement)
            return None

        try:
            # If there was a default schema when this query was run, then switch to it before trying to collect
            # the execution plan. This is necessary when the statement uses non-fully qualified tables
            # e.g. `select * from mytable` instead of `select * from myschema.mytable`
            if schema:
                cursor.execute('USE `{}`'.format(schema))
            self._log.debug('Using schema=%s', schema)
        except pymysql.err.DatabaseError as e:
            if len(e.args) != 2:
                raise
            if e.args[0] in PYMYSQL_NON_RETRYABLE_ERRORS:
                self._collection_strategy_cache[strategy_cache_key] = explain_strategy_error
            self._check.count("dd.mysql.statement_samples.error", 1,
                              tags=tags + ["error:explain-use-schema-{}".format(type(e))])
            self._log.debug(
                'Failed to collect execution plan because schema could not be accessed. error=%s, schema=%s, '
                'statement="%s"', e.args, schema, obfuscated_statement)
            return None

        # Use a cached strategy for the schema, if any, or try each strategy to collect plans
        strategies = list(self._preferred_explain_strategies)
        cached = self._collection_strategy_cache.get(strategy_cache_key)
        if cached:
            strategies.remove(cached)
            strategies.insert(0, cached)

        for strategy in strategies:
            try:
                plan = self._explain_strategies[strategy](cursor, statement)
                if plan:
                    self._collection_strategy_cache[strategy_cache_key] = strategy
                    self._log.debug(
                        'Successfully collected execution plan. strategy=%s, schema=%s, statement="%s"',
                        strategy, schema, obfuscated_statement)
                    return plan
            except pymysql.err.DatabaseError as e:
                if len(e.args) != 2:
                    raise
                if e.args[0] in PYMYSQL_NON_RETRYABLE_ERRORS:
                    self._collection_strategy_cache[strategy_cache_key] = explain_strategy_error
                self._check.count("dd.mysql.statement_samples.error", 1,
                                  tags=tags + ["error:explain-attempt-{}-{}".format(strategy, type(e))])
                self._log.debug(
                    'Failed to collect execution plan. error=%s, strategy=%s, schema=%s, statement="%s"',
                    e.args, strategy, schema, obfuscated_statement)
                continue

    def _run_explain(self, cursor, statement):
        """
        Run the explain using the EXPLAIN statement
        """
        cursor.execute('EXPLAIN FORMAT=json {}'.format(statement))
        return cursor.fetchone()[0]

    def _run_explain_procedure(self, cursor, statement):
        """
        Run the explain by calling the stored procedure if available.
        """
        cursor.execute('CALL {}(%s)'.format(self._explain_procedure), statement)
        return cursor.fetchone()[0]

    def _run_fully_qualified_explain_procedure(self, cursor, statement):
        """
        Run the explain by calling the fully qualified stored procedure if available.
        """
        cursor.execute('CALL {}(%s)'.format(self._fully_qualified_explain_procedure), statement)
        return cursor.fetchone()[0]

    @staticmethod
    def _can_explain(statement):
        # TODO: cleaner query cleaning to strip comments, etc.
        return statement.strip().split(' ', 1)[0].lower() in VALID_EXPLAIN_STATEMENTS

    @staticmethod
    def _parse_execution_plan_cost(execution_plan):
        """
        Parses the total cost from the execution plan, if set. If not set, returns cost of 0.
        """
        cost = json.loads(execution_plan).get('query_block', {}).get('cost_info', {}).get('query_cost', 0.0)
        return float(cost or 0.0)
