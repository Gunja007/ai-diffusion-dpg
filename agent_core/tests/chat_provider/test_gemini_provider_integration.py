import os
import pytest

from src.chat_provider.gemini_provider import GeminiChatProvider
from src.chat_provider.types import ChatRequest, Message, TextBlock

# Skip all tests in this file if API key is missing
pytestmark = pytest.mark.skipif(
    "GOOGLE_API_KEY" not in os.environ and "GEMINI_API_KEY" not in os.environ,
    reason="Missing Gemini API key",
)

@pytest.fixture
def real_provider():
    return GeminiChatProvider({
        "primary_model": "gemini-3.5-flash",
        "timeout_ms": 15000,
        "retry_attempts": 2,
    })

def test_gemini_integration_basic_call(real_provider):
    """Test a basic sync call to the real Gemini API."""
    req = ChatRequest(
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="Return the exact word 'SUCCESS' and nothing else.")],
            )
        ]
    )
    resp = real_provider.call(req)
    assert resp.stop_reason == "end_turn"
    assert len(resp.content) > 0
    assert "SUCCESS" in resp.content[0].text.upper()
