"""Synthetic large canonical upstream for the actual native app entrypoint."""

from skulk_openai import managed, native_managed
from tests_managed.continuation_child_fixture import fixture_transport

if __name__ == "__main__":
    managed.mock_transport = fixture_transport
    native_managed.main()
