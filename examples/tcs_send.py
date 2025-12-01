import asyncio
from pylabrobot.arms.precise_flex.precise_flex_api import PreciseFlexBackendApi

async def main():

    arm = PreciseFlexBackendApi()
    await arm.setup()
    while True:
        try:
            cmd = input("> ")
            reply = await arm.send_command(cmd)
            print(reply)
        except Exception as e:
            print(e)


if __name__ == '__main__':
    asyncio.run(main())