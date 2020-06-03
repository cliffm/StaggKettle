#!/usr/bin/env python3

import struct
from bluepy import btle
import paho.mqtt.client as mqtt
from enum import IntEnum
import json
import signal
from functools import partial
import sys
import asyncio

import config

# UUID of the remote service we wish to connect to.
# This is for the Fellow Stagg EKG+ SPS (serial over BLE) service.
SERVICE_UUID = "00001820-0000-1000-8000-00805f9b34fb"

# UUID of the characteristic for the serial channel. This shows up in wireshark
# as "Internet Protocol Support: Age"
CHARAC_UUID = "00002A80-0000-1000-8000-00805f9b34fb"

# sequence to send to interact with Kettle
INIT_SEQ = bytes.fromhex('efdd0b3031323334353637383930313233349a6d')

# sequence to turn Kettle on
POWER_ON_SEQ = bytes.fromhex('efdd0a0000010100')

# sequence to turn Kettle off
POWER_OFF_SEQ = bytes.fromhex('efdd0a0000000000')

# sequence to get temperature and state data from kettle
SETUP_DATA_SEQ = bytes.fromhex("0100")

# initial bytes for setting temperature
TARGET_TEMP_HDR = bytes.fromhex('efdd0a')

class AsyncioHelper:
    def __init__(self, loop, client):
        self.loop = loop
        self.client = client
        self.client.on_socket_open = self.on_socket_open
        self.client.on_socket_close = self.on_socket_close
        self.client.on_socket_register_write = self.on_socket_register_write
        self.client.on_socket_unregister_write = self.on_socket_unregister_write

    def on_socket_open(self, client, userdata, sock):

        def cb():
            client.loop_read()

        self.loop.add_reader(sock, cb)
        self.misc = self.loop.create_task(self.misc_loop())

    def on_socket_close(self, client, userdata, sock):
        self.loop.remove_reader(sock)
        self.misc.cancel()

    def on_socket_register_write(self, client, userdata, sock):
        def cb():
            client.loop_write()

        self.loop.add_writer(sock, cb)

    def on_socket_unregister_write(self, client, userdata, sock):
        self.loop.remove_writer(sock)

    async def misc_loop(self):
        while self.client.loop_misc() == mqtt.MQTT_ERR_SUCCESS:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break


class StaggKettle:
    
    class MessageTypes (IntEnum):
        POWER         = 0
        HOLD          = 1
        TARGET_TEMP   = 2
        CURRENT_TEMP  = 3
        COUNTDOWN     = 4
        UNK_1         = 5
        HOLDING       = 6
        UNK_2         = 7
        KETTLE_LIFTED = 8


    class MyDelegate(btle.DefaultDelegate):
        def __init__(self, mqtt_client):
            btle.DefaultDelegate.__init__(self)
            self.client = mqtt_client
            self._power = None 
            self._target_temp = None
            self._current_temp = None
            self._target_temp_scale = None
            self._current_temp_scale = None
            self.last_message_type = None
            self.changed = False

        def _on_change(self):
            self.changed = True

        @property
        def power(self):
            return self._power

        @power.setter
        def power(self, value):
            if self._power != value:
                self.client.publish("homeassistant/switch/kettle/state", value)
                self._on_change()
            self._power = value

        @property
        def target_temp(self):
            return self._target_temp

        @target_temp.setter
        def target_temp(self, value):
            if self._target_temp != value:
                self.client.publish("kitchen/kettle/temperature/target", value)
                self._on_change()
            self._target_temp = value

        @property
        def current_temp(self):
            return self._current_temp

        @current_temp.setter
        def current_temp(self, value):
            # valid values are 40 - 100 C, or 104 - 212 F
            # normally shows 32 when off
            if value < 40:
                value = "--"

            if self._current_temp != value:
                self.client.publish("homeassistant/sensor/kettle/temperature", value)
                self._on_change()
            self._current_temp = value

        @property
        def target_temp_scale(self):
            return self._target_temp_scale

        @target_temp_scale.setter
        def target_temp_scale(self, value):
            scale = '°F' if value == 1 else '°C'

            if self._target_temp_scale != scale:
                self.client.publish("kitchen/kettle/temperature/target/scale", scale)
                self._on_change()
            self._target_temp_scale = scale
            

        @property
        def current_temp_scale(self):
            return self._current_temp_scale

        @current_temp_scale.setter
        def current_temp_scale(self, value):
            scale = '°F' if value == 1 else '°C'

            if self._current_temp_scale != scale:
                self.client.publish("kitchen/kettle/temperature/scale", scale)
                self._on_change()
            self._current_temp_scale = scale
            
        def handleNotification(self, cHandle, data):
            if self.last_message_type is not None:
                if self.last_message_type == StaggKettle.MessageTypes.POWER:
                    power_value = struct.unpack('Bxx', data)[0]
                    if power_value == 0:
                        self.power = "off"
                    elif power_value == 1:
                        self.power = "on"
                    else:
                        print("Unknown Power Setting")
                        print(data)

                elif self.last_message_type == StaggKettle.MessageTypes.TARGET_TEMP:
                    (self.target_temp, self.target_temp_scale) = struct.unpack('BBxx', data)

                elif self.last_message_type == StaggKettle.MessageTypes.CURRENT_TEMP:
                    (self.current_temp, self.current_temp_scale) = struct.unpack('BBxx', data)
                
                # reset last_message
                self.last_message_type = None

            else:
                if data.startswith(b'\xef\xdd') and len(data) == 3:
                    self.last_message_type = struct.unpack('xxB', data)[0]
            
            if self.changed:
                print(f"Power: {self.power}\t"
                    f"Target Temp: {self.target_temp} {self.target_temp_scale}\t"
                    f"Current Temp: {self.current_temp} {self.current_temp_scale}")
                self.changed = False



    def __init__(self, kettle_address, loop):
        self.address = kettle_address
        self.loop = loop
        self.connected = False
        self.current_state = "off"
        self.client = mqtt.Client()
        self.setup_autodiscovery()


    def connect(self):
        if self.connected:
            return

        print("Connecting...")
        self.peripheral = btle.Peripheral(self.address)
        self.service = self.peripheral.getServiceByUUID(SERVICE_UUID)
        self.characteristics = self.peripheral.getCharacteristics(uuid=CHARAC_UUID)[0]
        self.characteristics.write(INIT_SEQ)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.will_set("kitchen/kettle/available", "offline", 0, True)

        if config.MQTT_PASS:
            self.client.username_pw_set(username=config.MQTT_USER, password=config.MQTT_PASS)


        self.client.connect(config.MQTT_BROKER, config.MQTT_PORT, 60)

        AsyncioHelper(self.loop, self.client)

        self.connected = True

    
    # The callback for when the client receives a CONNACK response from the server.
    def on_connect(self, client, userdata, flags, rc):
        client.publish("kitchen/kettle/available", "online", 0, True)

        client.subscribe("homeassistant/switch/kettle/set")
        client.subscribe("kitchen/kettle/temperature/set")
        

    # The callback for when a PUBLISH message is received from the server.
    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8")
        if payload == "on":
            self.power_on()
        elif payload == "off":
            self.power_off()
        
        client.publish("homeassistant/switch/kettle/state", payload)


    def power_on(self):
        if not self.connected:
            self.connect()
        
        print("Powering on")
        self.characteristics.write(POWER_ON_SEQ)
        self.current_state = "on"


    def power_off(self):
        if not self.connected:
            self.connect()
        
        print("Powering off")
        self.characteristics.write(POWER_OFF_SEQ)
        self.current_state = "off"


    def set_temperature(self, desired_temp):
        if not self.connected:
            self.connect()

        print(f"Setting temperature to {desired_temp}")
        seq_num = 0
        target_temp_body = struct.pack('BBBB', seq_num, desired_temp, seq_num + desired_temp % 256, 1)
            
        target_temp = TARGET_TEMP_HDR + target_temp_body
        self.characteristics.write(target_temp)


    def setup_autodiscovery(self):
        if not self.connected:
            self.connect()
        
        switch_config = {
            "name": "Kettle",
            "icon": "mdi:kettle",
            "state_topic": "homeassistant/switch/kettle/state",
            "command_topic": "homeassistant/switch/kettle/set",
            "availability_topic": "kitchen/kettle/available",
            "payload_on": "on",
            "payload_off": "off"
        }

        sensor_config = {
            "name" : "Kettle Temperature",
            "device_class": "temperature",
            "state_topic" : "homeassistant/sensor/kettle/temperature", 
            "availability_topic": "kitchen/kettle/available",
            "unit_of_measurement": "°F",
            "expire_after": 300
        }

        self.client.publish("homeassistant/switch/kettle/config", json.dumps(switch_config))
        self.client.publish("homeassistant/sensor/kettle/config", json.dumps(sensor_config))

    async def main_loop(self):
        self.connect()
        self.peripheral.withDelegate(StaggKettle.MyDelegate(self.client))

        notify_handle = self.characteristics.getHandle() + 1
        self.peripheral.writeCharacteristic(notify_handle, SETUP_DATA_SEQ, withResponse=True)
    
        self.client.loop_start()
        
        while True:
            self.peripheral.waitForNotifications(1.0)

#            self.client.loop_read()
#
#            if self.client.want_write():
#                self.client.loop_write()
#
#            self.client.loop_misc()


    def disconnect(self):
        print("Disconnecting...")
        self.power_off()
        self.peripheral.disconnect()
        self.connected = False


def signal_handler(sig, frame, obj):
    obj.disconnect()
    sys.exit(0)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    sk = StaggKettle(config.KETTLE_ADDRESS, loop)

    # clean up nicely
    signal.signal(signal.SIGINT, partial(signal_handler, obj=sk))
    
    loop.run_until_complete(sk.main_loop())
    loop.close()
