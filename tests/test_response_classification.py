"""no_response_kind: turning bridge pseudo-results into a decision.

This is the function that decides whether a command is safe to retry, and
whether the agent is looking at a real return value. It used to fall through to
None for the unload case, which made execute_js_rich report status:"success"
with the bridge's diagnostic string masquerading as the script's return value.
"""
from agent_browser_mcp.simphtml import no_response_kind


class TestRealReturns:
    def test_data_key_is_a_real_return(self):
        assert no_response_kind({"data": 2}) is None

    def test_explicit_null_return_is_still_a_return(self):
        # A script ending in `return null` must not look like a timeout.
        assert no_response_kind({"data": None}) is None

    def test_data_wins_even_when_result_also_present(self):
        assert no_response_kind({"data": 1, "result": "Session x reloaded."}) is None


class TestUndelivered:
    """Never reached the page, so a retry cannot double a side effect."""

    def test_no_ack(self):
        assert no_response_kind(
            {"result": "No response data in 15s (no ACK, script may not have been delivered)"}
        ) == "undelivered"

    def test_http_never_polled(self):
        assert no_response_kind(
            {"result": "Session a:1 no response in 15s (script not polled)"}
        ) == "undelivered"


class TestAfterAck:
    """Delivered and possibly still running: retrying could double a submit."""

    def test_ack_received(self):
        assert no_response_kind(
            {"result": "No response data in 15s (ACK received, script may still be running)"}
        ) == "after_ack"

    def test_http_delivered_no_result(self):
        assert no_response_kind(
            {"result": "Session a:1 no response in 15s (delivered but no result)"}
        ) == "after_ack"


class TestNavigated:
    """The regression this suite exists for: click -> navigation.

    The page unloaded before the result came back. The script DID run, so this
    is neither a failure nor a plain success, and the return value is gone.
    """

    def test_unload_mid_flight(self):
        assert no_response_kind(
            {"result": "Session a:1 reloaded.", "closed": 1}
        ) == "navigated"

    def test_unload_then_timeout(self):
        assert no_response_kind(
            {"result": "Session a:1 reloaded and new page is loading...", "closed": 1}
        ) == "navigated"

    def test_navigated_is_not_undelivered(self):
        # Misclassifying this as undelivered would re-run a form submit.
        kind = no_response_kind({"result": "Session a:1 reloaded.", "closed": 1})
        assert kind != "undelivered"


class TestMalformed:
    def test_not_a_dict(self):
        assert no_response_kind("Session x reloaded.") is None
        assert no_response_kind(None) is None
        assert no_response_kind([1, 2]) is None

    def test_result_not_a_string(self):
        assert no_response_kind({"result": 42}) is None
        assert no_response_kind({"result": None}) is None

    def test_empty_dict(self):
        assert no_response_kind({}) is None

    def test_unrecognised_message_is_not_guessed(self):
        assert no_response_kind({"result": "something nobody planned for"}) is None
