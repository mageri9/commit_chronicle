import asyncio, json
from src.storage.cache import get_redis

async def check():
    redis = await get_redis()
    keys = await redis.keys('job_message:*')
    print(f'Ключей: {len(keys)}')
    for k in keys:
        raw = await redis.get(k)
        print(f'{k}: {raw}')

asyncio.run(check())
