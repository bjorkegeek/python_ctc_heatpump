import asyncio
import calendar
import datetime
import re
import time

from aioserial import AioSerial

Pattern = type(re.compile(''))

# Thanks stackoverflow
gsm = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
       "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
ext = ("````````````````````^```````````````````{}`````\\````````````[~]`"
       "|````````````````````````````````````€``````````````````````````")

def gsm_encode(plaintext):
    res = b""
    for c in plaintext:
        idx = gsm.find(c);
        if idx != -1:
            res += bytes([idx])
            continue
        idx = ext.find(c)
        if idx != -1:
            res += b'\x1b' + bytes([idx])
    return res

def gsm_decode(gsm_bytes):
    i = 0
    ret = ""
    bit = iter(gsm_bytes)
    for byte in bit:
        if byte == b'\x1b':
            ret += ext[next(bit)]
        else:
            ret += gsm[byte]
    return ret

async def read_async_and_echo(ser, *args, **kwargs):
    b = await ser.read_async(*args, **kwargs)
    await ser.write_async(b)
    return b

async def get_at_command(ser):
    wait_for = b'a'
    ret = b''
    while 1:
        #print('wait_for', wait_for)
        b = await read_async_and_echo(ser, 1)
        #print('got', repr(b))
        if b == wait_for:
            if wait_for == b"a":
                ret += b
                wait_for = b"t"
            elif wait_for == b"t":
                ret += b
                break
            else:
                wait_for = b'a'
        else:
            ret = b''
            wait_for = b'\n'
    while b != b'\n':
        b = await read_async_and_echo(ser, 1)
        #print('got', repr(b), 'collecting')
        ret += b
    return ret

modem_commands = {
    b'at+cpms="MT"\r\n': '_sms_request',
    b'at+cmgf=1\r\n': '_mode_change',
    b'at+cmgl="REC UNREAD"\r\n': '_list_unread',
    re.compile(b'at\\+cmgd=\\s*(\\d+)?\r\n'): '_delete_text',
    re.compile(b'at\\+cmgs="(.*)"\r\n'): '_send_text',
}

NUM_TEXT_SLOTS = 16


class Heatpump:
    def __init__(self, *args, on_message=lambda x: None, **kwargs):
        """
        Initializes Heatpump object. Keyword argument on_message may be
        set to a callback that will be scheduled every time a status update
        is received from the heat pump, with the status message string as argument

        Other args and kwargs are passed on to the AioSerial constructor
        """
        self._serial = AioSerial(*args, **kwargs)
        self._is_run = False
        self._read_text = False
        self._outstanding_state_request = None
        self._temperature_change_request = None
        self._activated = False
        self._on_message = on_message

    async def run(self):
        """
        Main loop entry point. Will not return. Run it using asyncio.create_task
        if ability to cancel is desired
        """
        if self._is_run:
            raise RuntimeError("Machine can only run once")
        self._is_run = True
        try:
            self._serial.rts = True
            self._serial.dtr = True
            while 1:
                at_command = await get_at_command(self._serial)

                method = modem_commands.get(at_command)
                if method:
                    await getattr(self, method)()
                    continue
                for key, method in modem_commands.items():
                    if isinstance(key, Pattern):
                        mo = key.match(at_command)
                        if mo:
                            await getattr(self, method)(mo)
                            break
                else:
                    #print("Got unknown command", repr(at_command))
                    await self._serial.write_async(b"ERROR\r\n")
        finally:
            self._serial.rts = False
            self._serial.dtr = False

    def set_temperature(self, temperature):
        """
        Request that the heat pump changes its desired temperature.
        This function will return immediately, the only way to get an ack
        is using the status message string callback.
        """
        if not isinstance(temperature, int) or temperature < 0:
            raise TypeError("Expected temperature to be positive integer")
        self._temperature_change_request = temperature


    async def _mode_change(self):
        #print("Got mode change command")
        await self._serial.write_async(b"OK\r\n")

    async def _sms_request(self):
        #print("Got SMS request command")
        slot_size = NUM_TEXT_SLOTS

        slots_with_texts = 1 if self._outstanding_state_request is None else 0
        a = 3 * [slot_size, slots_with_texts]

        reply = "+CPMS: {},{},{},{},{},{}\r\n".format(*a).encode("ascii")
        #print("Reply: ", repr(reply))
        await self._serial.write_async(reply)

    async def _list_unread(self):
        #print("Got list unread command")
        if not self._outstanding_state_request:
            slot = 1
            if not self._activated:
                message = "aktiveranummer"
            elif self._temperature_change_request is not None:
                message = f"rum{self._temperature_change_request}"
            else:
                message = "driftdata"

            gsmoffset = int((calendar.timegm(time.localtime()) -
                             calendar.timegm(time.gmtime())) / 900)
            #print("Telling about message", slot, ":", message)
            reply = '+CMGL: {},"REC UNREAD","{}",,"{}{:+03}"\r\n'.format(
                slot, "+46701111111",
                datetime.datetime.now().strftime("%y/%m/%d,%H:%M:%S"),
                gsmoffset)
            #print(" ", reply)
            self._read_text = True
            await self._serial.write_async(reply.encode("ascii"))
            await self._serial.write_async(gsm_encode(message) + b"\r\n")
            await self._serial.write_async(b"\r\n")

        await self._serial.write_async(b"OK\r\n")

    async def _delete_text(self, mo):
        slot = int(mo.group(1))
        slot0 = slot - 1
        if slot0 >= 0 and slot0 < NUM_TEXT_SLOTS:
            #print("Deleting text", slot)
            if slot == 1:
                if not self._read_text:
                    #print("Text not read, do nothing")
                    pass
                elif not self._activated:
                    #print("Ok, activated!")
                    self._activated = True
                elif self._temperature_change_request is not None:
                    #print("Ack temperature change request")
                    self._temperature_change_request = None

                self._read_text = False
            await self._serial.write_async(b"OK\r\n")
        else:
            await self._serial.write_async(b"ERROR\r\n")

    async def _send_text(self, mo):
        #print("Send text to ", to_number)
        st = b""
        while 1:
            b = await read_async_and_echo(self._serial, 1)
            st = st + b
            if b == b"\n":
                break

        message = gsm_decode(st)
        defer(lambda: self._on_message(message))
        self._outstanding_state_request = False


def defer(fn):
    asyncio.get_running_loop().call_soon(fn)
