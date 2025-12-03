import asyncio
from pylabrobot.arms.precise_flex.pf_400 import PreciseFlex400Backend

HOST = "192.168.1.100"  # Update with actual robot IP

async def main():

    arm = PreciseFlex400Backend(host=HOST)
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