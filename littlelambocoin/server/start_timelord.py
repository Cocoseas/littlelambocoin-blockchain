import logging
import pathlib
from typing import Dict

from littlelambocoin.consensus.constants import ConsensusConstants
from littlelambocoin.consensus.default_constants import DEFAULT_CONSTANTS
from littlelambocoin.rpc.timelord_rpc_api import TimelordRpcApi
from littlelambocoin.server.outbound_message import NodeType
from littlelambocoin.server.start_service import run_service
from littlelambocoin.timelord.timelord import Timelord
from littlelambocoin.timelord.timelord_api import TimelordAPI
from littlelambocoin.types.peer_info import PeerInfo
from littlelambocoin.util.config import load_config_cli
from littlelambocoin.util.default_root import DEFAULT_ROOT_PATH

# See: https://bugs.python.org/issue29288
"".encode("idna")

SERVICE_NAME = "timelord"


log = logging.getLogger(__name__)


def service_kwargs_for_timelord(
    root_path: pathlib.Path,
    config: Dict,
    constants: ConsensusConstants,
) -> Dict:

    connect_peers = [PeerInfo(config["full_node_peer"]["host"], config["full_node_peer"]["port"])]
    overrides = config["network_overrides"]["constants"][config["selected_network"]]
    updated_constants = constants.replace_str_to_bytes(**overrides)

    node = Timelord(root_path, config, updated_constants)
    peer_api = TimelordAPI(node)
    network_id = config["selected_network"]
    kwargs = dict(
        root_path=root_path,
        peer_api=peer_api,
        node=node,
        node_type=NodeType.TIMELORD,
        advertised_port=config["port"],
        service_name=SERVICE_NAME,
        server_listen_ports=[config["port"]],
        connect_peers=connect_peers,
        auth_connect_peers=False,
        network_id=network_id,
    )

    if config.get("start_rpc_server", True):
        kwargs["rpc_info"] = (TimelordRpcApi, config.get("rpc_port", 8557))

    return kwargs


def main() -> None:
    config = load_config_cli(DEFAULT_ROOT_PATH, "config.yaml", SERVICE_NAME)
    kwargs = service_kwargs_for_timelord(DEFAULT_ROOT_PATH, config, DEFAULT_CONSTANTS)
    return run_service(**kwargs)


if __name__ == "__main__":
    main()
