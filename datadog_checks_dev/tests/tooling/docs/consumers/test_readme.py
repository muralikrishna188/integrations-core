# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import mock
import pytest

from ..utils import get_readme_consumer, normalize_yaml, MOCK_RESPONSE

pytestmark = [pytest.mark.conf, pytest.mark.conf_consumer]


@mock.patch('datadog_checks.dev.tooling.specs.docs.spec.load_manifest', return_value=MOCK_RESPONSE)
def test_tab_valid(_):

    consumer = get_readme_consumer(
        """
        name: foo
        files:
        - name: README.md
          sections:
          - name: foo
            header_level: 1
            description: words
            tab: bar
        """
    )
    files = consumer.render()
    contents, errors = files['README.md']
    assert not errors
    assert contents == normalize_yaml(
        """
        # Agent Check: foo

        <!-- xxx tabs xxx -->
        <!-- xxx tab "bar" xxx -->

        # foo

        words

        <!-- xxz tab xxx -->
        <!-- xxz tabs xxx -->

        """
    )


@mock.patch('datadog_checks.dev.tooling.specs.docs.spec.load_manifest', return_value=MOCK_RESPONSE)
def test_tab_multiple(_):

    consumer = get_readme_consumer(
        """
        name: foo
        files:
        - name: README.md
          sections:
          - name: foo
            header_level: 1
            description: words
            tab: bar
          - name: bar
            header_level: 1
            description: words
            tab: baz
        """
    )
    files = consumer.render()
    contents, errors = files['README.md']
    assert not errors
    assert contents == normalize_yaml(
        """
        # Agent Check: foo

        <!-- xxx tabs xxx -->
        <!-- xxx tab "bar" xxx -->

        # foo

        words

        <!-- xxz tab xxx -->
        <!-- xxx tab "baz" xxx -->

        # bar

        words

        <!-- xxz tab xxx -->
        <!-- xxz tabs xxx -->

        """
    )
