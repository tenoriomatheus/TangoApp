from common.file import file_helper
from client.network.network_data import NetworkData
from client.data_paths import base_dir, net_dir
from Crypto.Random import get_random_bytes
import pathlib


def test_network_data():
    name = 'test_net'
    key = get_random_bytes(16)
    key_index = get_random_bytes(2)
    iv_index = get_random_bytes(4)
    num_apps = 10

    apps = []
    for x in range(num_apps):
        apps.append(f'test_app{x}')

    data = NetworkData(name=name, key=key, key_index=key_index,
                       iv_index=iv_index, seq=-1, apps=apps)

    assert file_helper.file_exist(base_dir + net_dir + name + '.yml') is \
        False

    data.save()

    assert file_helper.file_exist(base_dir + net_dir + name + '.yml') is \
        True

    r_data = NetworkData.load(base_dir + net_dir + name + '.yml')

    assert data == r_data


def test_cleanup():
    pathlib.Path(base_dir + net_dir + 'test_net.yml').unlink()