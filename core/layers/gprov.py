from core.link import Link
from core.buffer import Buffer
from core.utils import threaded
from time import time
from threading import Event
from dataclasses import dataclass
from typing import Any


LINK_OPEN = 0x00
LINK_ACK = 0x01
LINK_CLOSE = 0x02

START = 0x00
ACKNOWLEDGMENT = 0x01
CONTINUATION = 0x02
BEARER_CONTROL = 0x03

MAX_MTU_SIZE = 24


@dataclass
class GProvStart:
    seg_n: int
    total_length: int
    fcs: int
    content: bytes


@dataclass
class GProvContinuation:
    seg_index: int
    content: bytes


@dataclass
class GProvAck:
    content: bytes


@dataclass
class GProvBearerControl:
    op_code: int
    content: bytes


@dataclass
class GProvData:
    type_: int
    msg_params: Any


def decode_msg_start(buffer: Buffer):
    first_byte = buffer.pull_u8()
    seg_n = (first_byte & 0b1111_1100) >> 2

    total_length = buffer.pull_be16()
    fcs = buffer.pull_u8()
    content = buffer.buffer

    return GProvStart(seg_n, total_length, fcs, content)


def decode_msg_ack(buffer: Buffer):
    first_byte = buffer.pull_u8()
    padding = (first_byte & 0b1111_1100) >> 2
    if padding != 0:
        raise Exception('Padding of Ack message is not zero')

    return GProvAck(b'')


def decode_msg_continuation(buffer: Buffer):
    first_byte = buffer.pull_u8()
    seg_index = (first_byte & 0b1111_1100) >> 2
    content = buffer.buffer

    return GProvContinuation(seg_index, content)


def decode_msg_bearer_control(buffer: Buffer):
    first_byte = buffer.pull_u8()
    op_code = (first_byte & 0b1111_1100) >> 2

    if op_code == LINK_OPEN:
        uuid = buffer.buffer
        return GProvBearerControl(op_code, uuid)
    elif op_code == LINK_CLOSE:
        close_reason = buffer.pull_u8()
        return GProvBearerControl(op_code, close_reason)
    elif op_code == LINK_ACK:
        return GProvBearerControl(op_code, b'')
    else:
        raise Exception('Bearer Op code Unknown')


def decode_gprov_message(buffer: Buffer):
    type_ = buffer.seek(0) & 0b0000_0011

    if type_ == START:
        func_output = decode_msg_start(buffer)
    elif type_ == ACKNOWLEDGMENT:
        func_output = decode_msg_ack(buffer)
    elif type_ == CONTINUATION:
        func_output = decode_msg_continuation(buffer)
    elif type_ == BEARER_CONTROL:
        func_output = decode_msg_bearer_control(buffer)
    else:
        raise Exception('Generic Provisioning message unknown')

    return GProvData(type_, func_output)


# def encode_msg_bearer_control(self, link: Link, bearer_op):
#     op_code = ((bearer_op.value << 2) | 0b0000_0011)
#     self.header.push_u8(op_code)
#
#     if op_code == LINK_OPEN:
#         self.payload.push_be(link.device_uuid.address)
#     elif op_code == LINK_CLOSE:
#         self.payload.push_be(link.close_reason)


class GProvLayer:

    def __init__(self, pb_adv_layer):
        self.__pb_adv_layer = pb_adv_layer
        self.__ack_recv_event = Event()
        self.__ack_timeout_event = Event()

    # TODO: implement open link method
    def open(self, link: Link):
        pass

    # TODO: Implement FCS method
    def __fcs(self, buffer):
        return 0

    @threaded
    def __check_tr_ack(self):
        buffer = Buffer()
        type_ = START
        start_time = time()
        elapsed_time = time()

        while type_ != ACKNOWLEDGMENT and elapsed_time < 30:
            content = self.__pb_adv_layer.recv(1, 0.5)
            buffer.clear()
            buffer.push_be(content)
            type_ = decode_gprov_message(buffer)
            elapsed_time += start_time - time()

        if type_ == ACKNOWLEDGMENT and elapsed_time < 30:
            self.__ack_recv_event.set()

        self.__ack_timeout_event.set()

    def __send_start_tr(self, link, buffer):
        # get_total_length
        total_length = buffer.length
        # get fcs
        fcs = self.__fcs(buffer)
        # get start segment
        start_content = buffer.pull_be(MAX_MTU_SIZE - 4)
        # get continuation_segments
        continuation_segments = []
        while buffer.length > (MAX_MTU_SIZE - 1):
            continuation_segments.append(buffer.pull_be(MAX_MTU_SIZE - 1))
        continuation_segments.append(buffer.pull_all_be())
        # get segN value
        seg_n = len(continuation_segments)
        last_seg_number = (seg_n & 0b0011_1111) << 2

        msg = Buffer()
        msg.push_u8(last_seg_number)
        msg.push_be16(total_length)
        msg.push_u8(fcs)
        msg.push_be(start_content)

        self.__pb_adv_layer.send(link, msg.buffer_be())

        return continuation_segments

    def __send_continuation_tr(self, link, continuation_segments):
        msg = Buffer()

        for x in range(len(continuation_segments)):
            msg.clear()

            seg_index = (x+1 & 0b0011_1111) << 2 | 0b0000_0010
            msg.push_u8(seg_index)
            msg.push_be(continuation_segments[x])

            self.__pb_adv_layer.send(link, msg.buffer_be())

    def send(self, link: Link, content: bytes):
        buffer = Buffer()
        buffer.push_be(content)

        # send start transaction
        continuation_segments = self.__send_start_tr(link, buffer)

        # start thread to check ack response
        self.__check_tr_ack()

        # send
        self.__send_continuation_tr(link, continuation_segments)

        # wait ack response
        self.__ack_timeout_event.wait(35)
        # check if ack was received
        if not self.__ack_recv_event.is_set():
            # cancel tr, provisioning and close link
            raise Exception('No ack message received within 30 seconds')

    def recv(self):
        buffer = Buffer()
        buffer.push_be(self.__pb_adv_layer.recv('prov'))

        gprov_data = decode_gprov_message(buffer)
        return gprov_data.msg_params.content

    # TODO: implement close link method
    def close(self, link: Link):
        pass
