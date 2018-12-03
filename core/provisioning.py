from core.utils import timer
from threading import Event
from data_structs.buffer import Buffer
from core.device import Capabilities
from ecdsa import SigningKey, NIST256p
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Hash import CMAC
from model.database import nets

PROVISIONING_INVITE = 0x00
PROVISIONING_CAPABILITIES = 0x01
PROVISIONING_START = 0x02
PROVISIONING_PUBLIC_KEY = 0x03
PROVISIONING_INPUT_COMPLETE = 0x04
PROVISIONING_CONFIRMATION = 0x05
PROVISIONING_RANDOM = 0x06
PROVISIONING_DATA = 0x07
PROVISIONING_COMPLETE = 0x08
PROVISIONING_FAILED = 0x09

CLOSE_SUCCESS = b'\x00'
CLOSE_TIMEOUT = b'\x01'
CLOSE_FAIL = b'\x02'


class ProvisioningFail(Exception):
    pass


class ProvisioningTimeout(Exception):
    pass


class ProvisioningLayer:

    def __init__(self, gprov_layer, dongle_driver):
        self.__gprov_layer = gprov_layer
        self.__dongle_driver = dongle_driver
        self.__device_capabilities = None
        self.__priv_key = None
        self.__pub_key = None
        self.__device_pub_key = None
        self.__ecdh_secret = None
        self.__sk = None
        self.__vk = None
        self.__provisioning_invite = None
        self.__provisioning_capabilities = None
        self.__provisioning_start = None
        self.__auth_value = None
        self.__random_provisioner = None
        self.__random_device = None
        self.default_attention_duration = 5
        self.public_key_type = 0x00
        self.authentication_method = 0x00
        self.authentication_action = 0x00
        self.authentication_size = 0x00

    def scan(self, timeout=None):
        device = None

        if timeout is not None:
            scan_timeout_event = Event()
            timer(timeout, scan_timeout_event)
            while not scan_timeout_event.is_set():
                content = self.__dongle_driver.recv('beacon', 1, 0.5)
                if content is not None:
                    device = self.__process_beacon_content(content)
                    break
        else:
            content = self.__dongle_driver.recv('beacon')
            device = self.__process_beacon_content(content)

        return device

    def provisioning_device(self, device_uuid: bytes, net_name: str):
        self.__gprov_layer.open_link(device_uuid)

        try:
            self.__invitation_prov_phase()
            self.__exchanging_pub_keys_prov_phase()
            self.__authentication_prov_phase()
            self.__send_data_prov_phase(net_name)

            self.__gprov_layer.close_link(CLOSE_SUCCESS)
        except ProvisioningFail:
            self.__gprov_layer.close_link(CLOSE_FAIL)
        except ProvisioningTimeout:
            self.__gprov_layer.close_link(CLOSE_TIMEOUT)

    # TODO: change this to get only device uuid
    @staticmethod
    def __process_beacon_content(content: bytes):
        return content.split(b' ')[1]

    def __invitation_prov_phase(self):
        # send prov invite
        send_buff = Buffer()
        send_buff.push_u8(PROVISIONING_INVITE)
        send_buff.push_u8(self.default_attention_duration)
        self.__gprov_layer.send_transaction(send_buff.buffer_be())

        self.__provisioning_invite = self.default_attention_duration

        # recv prov capabilities
        recv_buff = Buffer()
        content = self.__gprov_layer.get_transaction()
        recv_buff.push_be(content)
        opcode = recv_buff.pull_u8()
        self.__provisioning_capabilities = recv_buff.buffer_be()
        if opcode != PROVISIONING_CAPABILITIES:
            raise ProvisioningFail()
        self.__device_capabilities = Capabilities(recv_buff)

    def __exchanging_pub_keys_prov_phase(self):
        # send prov start (No OOB)
        start_buff = Buffer()
        start_buff.push_u8(PROVISIONING_START)
        start_buff.push_u8(0x00)
        start_buff.push_u8(self.public_key_type)
        start_buff.push_u8(self.authentication_method)
        start_buff.push_u8(self.authentication_action)
        start_buff.push_u8(self.authentication_size)
        self.__provisioning_start = start_buff.buffer_be()[1:]
        self.__auth_value = b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        self.__gprov_layer.send_transaction(start_buff.buffer_be())

        # gen priv_key and pub_key
        self.__gen_keys()

        # send my pub key
        pub_key_buff = Buffer()
        pub_key_buff.push_u8(PROVISIONING_PUBLIC_KEY)
        pub_key_buff.push_be(self.__pub_key['x'])
        pub_key_buff.push_be(self.__pub_key['y'])
        self.__gprov_layer.send_transaction(pub_key_buff.buffer_be())

        # recv device pub key
        recv_buff = Buffer()
        content = self.__gprov_layer.get_transaction()
        recv_buff.push_be(content)
        opcode = recv_buff.pull_u8()
        if opcode != PROVISIONING_PUBLIC_KEY:
            raise ProvisioningFail()
        self.__device_pub_key = {
            'x': recv_buff.pull_be(32),
            'y': recv_buff.pull_be(32)
        }

        # calc ecdh_secret = P-256(priv_key, dev_pub_key)
        self.__calc_ecdh_secret()

    def __authentication_prov_phase(self):
        buff = Buffer()

        # calc crypto values need
        confirmation_inputs = self.__provisioning_invite + self.__provisioning_capabilities + \
                              self.__provisioning_start + self.__pub_key['x'] + self.__pub_key['y'] + \
                              self.__device_pub_key['x'] + self.__device_pub_key['y']
        self.__confirmation_salt = self.__s1(confirmation_inputs)
        confirmation_key = self.__k1(self.__ecdh_secret['x'], self.__confirmation_salt, b'prck')
        # confirmation_key = self.__k1(self.__ecdh_secret['x'] + self.__ecdh_secret['y'], self.__confirmation_salt,
        #                              b'prck')

        self.__gen_random_provisioner()

        # send confirmation provisioner
        confirmation_provisioner = self.__aes_cmac(confirmation_key, self.__random_provisioner + self.__auth_value)
        buff.push_be(confirmation_provisioner)
        self.__gprov_layer.send_transaction(buff.buffer_be())

        # recv confiramtion device
        recv_confirmation_device = self.__recv(opcode_verification=PROVISIONING_CONFIRMATION)

        # send random provisioner
        buff.clear()
        buff.push_be(self.__random_provisioner)
        self.__gprov_layer.send_transaction(buff.buffer_be())

        # recv random device
        self.__random_device = self.__recv(opcode_verification=PROVISIONING_RANDOM)

        # check info
        calc_confiramtion_device = self.__aes_cmac(confirmation_key, self.__random_device + self.__auth_value)

        if recv_confirmation_device != calc_confiramtion_device:
            raise ProvisioningFail()

    def __send_data_prov_phase(self, net_name):
        net = nets.get(net_name)

        net_key = net.net_key
        key_index = int(net.net_key_index).to_bytes(2, 'big')
        flags = b'\x00'
        iv_index = net.iv_index
        unicast_address = net.next_unicast_address()

        provisioning_salt = self.__s1(self.__confirmation_salt + self.__random_provisioner + self.__random_device)
        session_key = self.__k1(self.__ecdh_secret['x'], provisioning_salt, b'prsk')
        session_nonce = self.__k1(self.__ecdh_secret['x'], provisioning_salt, b'prsn')
        # session_key = self.__k1(self.__ecdh_secret['x'] + self.__ecdh_secret['y'], self.__provisioning_salt, b'prsk')
        # session_nonce = self.__k1(self.__ecdh_secret['x'] + self.__ecdh_secret['y'], self.__provisioning_salt,
                                  # b'prsn')
        provisioning_data = net_key + key_index + flags + iv_index + unicast_address

        encrypted_provisioning_data, provisioning_data_mic = self.__aes_ccm(session_key, session_nonce,
                                                                            provisioning_data)

        buff = Buffer()
        buff.push_be(encrypted_provisioning_data)
        buff.push_be(provisioning_data_mic)
        self.__gprov_layer.send_transaction(buff.buffer_be())

        self.__recv(opcode_verification=PROVISIONING_CONFIRMATION)

    def __recv(self, opcode_verification=None):
        buff = Buffer()
        buff.push_be(self.__gprov_layer.get_transaction())
        opcode = buff.pull_u8()
        content = buff.buffer_be()
        if opcode == PROVISIONING_FAILED:
            raise ProvisioningFail()
        if opcode_verification is not None:
            if opcode != opcode_verification:
                raise ProvisioningFail()
            return content
        else:
            return opcode, content

    def __gen_keys(self):

        self.__sk = SigningKey.generate(curve=NIST256p)
        self.__vk = self.__sk.get_verifying_key()

        self.__priv_key = self.__sk.to_string()
        self.__pub_key = {
            'x': self.__vk.to_string()[0:32],
            'y': self.__vk.to_string()[32:64]
        }

    # TODO: ECDHsecret is 32 bytes or 64 bytes
    def __calc_ecdh_secret(self):
        secret = self.__sk.privkey.secret_multiplier * self.__vk.pubkey.point

        self.__ecdh_secret = {
            'x': secret.x().to_bytes(32, 'big'),
            'y': secret.y().to_bytes(32, 'big')
        }

    def __s1(self, input_: bytes):
        zero = b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        return self.__aes_cmac(zero, input_)

    def __k1(self, shared_secret: bytes, salt: bytes, msg: bytes):
        okm = self.__aes_cmac(salt, shared_secret)
        return self.__aes_cmac(okm, msg)

    def __gen_random_provisioner(self):
        self.__random_provisioner = get_random_bytes(16)

    def __aes_cmac(self, key: bytes, msg: bytes):
        cipher = CMAC.new(key, ciphermod=AES)
        cipher.update(msg)
        return cipher.digest()

    def __aes_ccm(self, key, nonce, data):
        cipher = AES.new(key, AES.MODE_CCM, nonce)
        return cipher.encrypt(data), cipher.digest()
