import asyncio
import json
import time

DELAY = 0.3


async def bridge(reader, writer):
    upstream, target = await asyncio.open_connection("echo", 8080)
    connection = str(writer.get_extra_info("peername"))

    async def to_backend():
        while data := await reader.read(65536):
            target.write(data)
            await target.drain()

    async def to_envoy():
        while data := await upstream.read(65536):
            writer.write(data)
            await writer.drain()
        print(
            json.dumps(
                {
                    "event": "backend_eof",
                    "connection": connection,
                    "at": time.monotonic(),
                    "delay_seconds": DELAY,
                }
            ),
            flush=True,
        )
        await asyncio.sleep(DELAY)
        writer.close()

    tasks = [asyncio.create_task(to_backend()), asyncio.create_task(to_envoy())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except ConnectionError, OSError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        target.close()
        await asyncio.gather(writer.wait_closed(), target.wait_closed(), return_exceptions=True)


async def main():
    server = await asyncio.start_server(bridge, "0.0.0.0", 9000)
    print("FIN-delay proxy ready", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
