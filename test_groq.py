import asyncio
from agents._groq_client import _call_groq

async def test():
    res = await _call_groq(
        "You are a test assistant",
        "Return JSON with key 'status' = 'working'"
    )
    print(res)

asyncio.run(test())