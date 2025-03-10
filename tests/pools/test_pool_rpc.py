import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from typing import Optional, List, Dict

import pytest
import pytest_asyncio
from blspy import G1Element

from littlelambocoin.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from littlelambocoin.pools.pool_puzzles import SINGLETON_LAUNCHER_HASH
from littlelambocoin.pools.pool_wallet_info import PoolWalletInfo, PoolSingletonState
from littlelambocoin.protocols import full_node_protocol
from littlelambocoin.protocols.full_node_protocol import RespondBlock
from littlelambocoin.rpc.rpc_server import start_rpc_server
from littlelambocoin.rpc.wallet_rpc_api import WalletRpcApi
from littlelambocoin.rpc.wallet_rpc_client import WalletRpcClient
from littlelambocoin.simulator.simulator_protocol import FarmNewBlockProtocol, ReorgProtocol
from littlelambocoin.types.blockchain_format.sized_bytes import bytes32

from littlelambocoin.types.peer_info import PeerInfo
from littlelambocoin.util.bech32m import encode_puzzle_hash
from littlelambocoin.util.byte_types import hexstr_to_bytes
from littlelambocoin.wallet.derive_keys import find_authentication_sk, find_owner_sk
from littlelambocoin.wallet.wallet_node import WalletNode
from tests.block_tools import get_plot_dir
from littlelambocoin.util.config import load_config
from littlelambocoin.util.ints import uint16, uint32
from littlelambocoin.wallet.transaction_record import TransactionRecord
from littlelambocoin.wallet.util.wallet_types import WalletType
from tests.setup_nodes import self_hostname, setup_simulators_and_wallets, bt
from tests.time_out_assert import time_out_assert
from tests.util.socket import find_available_listen_port

# TODO: Compare deducted fees in all tests against reported total_fee
log = logging.getLogger(__name__)
FEE_AMOUNT = 2000000000000


def get_pool_plot_dir():
    return get_plot_dir() / Path("pool_tests")


@dataclass
class TemporaryPoolPlot:
    p2_singleton_puzzle_hash: bytes32
    plot_id: Optional[bytes32] = None

    async def __aenter__(self):
        plot_id: bytes32 = await bt.new_plot(self.p2_singleton_puzzle_hash, get_pool_plot_dir())
        assert plot_id is not None
        await bt.refresh_plots()
        self.plot_id = plot_id
        return self

    async def __aexit__(self, exc_type, exc_value, exc_traceback):
        await bt.delete_plot(self.plot_id)


async def create_pool_plot(p2_singleton_puzzle_hash: bytes32) -> Optional[bytes32]:
    plot_id = await bt.new_plot(p2_singleton_puzzle_hash, get_pool_plot_dir())
    await bt.refresh_plots()
    return plot_id


async def wallet_is_synced(wallet_node: WalletNode, full_node_api):
    assert wallet_node.wallet_state_manager is not None
    return (
        await wallet_node.wallet_state_manager.blockchain.get_finished_sync_up_to()
        == full_node_api.full_node.blockchain.get_peak_height()
    )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop


PREFARMED_BLOCKS = 4


class TestPoolWalletRpc:
    @pytest_asyncio.fixture(scope="function")
    async def two_wallet_nodes(self):
        async for _ in setup_simulators_and_wallets(1, 2, {}):
            yield _

    @pytest_asyncio.fixture(scope="function")
    async def one_wallet_node_and_rpc(self):
        rmtree(get_pool_plot_dir(), ignore_errors=True)
        async for nodes in setup_simulators_and_wallets(1, 1, {}):
            full_nodes, wallets = nodes
            full_node_api = full_nodes[0]
            wallet_node_0, wallet_server_0 = wallets[0]

            wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
            our_ph = await wallet_0.get_new_puzzlehash()
            await self.farm_blocks(full_node_api, our_ph, PREFARMED_BLOCKS)

            api_user = WalletRpcApi(wallet_node_0)
            config = bt.config
            daemon_port = config["daemon_port"]
            test_rpc_port = find_available_listen_port("rpc_port")

            rpc_cleanup = await start_rpc_server(
                api_user,
                self_hostname,
                daemon_port,
                test_rpc_port,
                lambda x: None,
                bt.root_path,
                config,
                connect_to_daemon=False,
            )
            client = await WalletRpcClient.create(self_hostname, test_rpc_port, bt.root_path, config)

            yield client, wallet_node_0, full_node_api

            client.close()
            await client.await_closed()
            await rpc_cleanup()

    @pytest_asyncio.fixture(scope="function")
    async def setup(self, two_wallet_nodes):
        rmtree(get_pool_plot_dir(), ignore_errors=True)
        full_nodes, wallets = two_wallet_nodes
        wallet_node_0, wallet_server_0 = wallets[0]
        wallet_node_1, wallet_server_1 = wallets[1]
        our_ph_record = await wallet_node_0.wallet_state_manager.get_unused_derivation_record(1, False, True)
        pool_ph_record = await wallet_node_1.wallet_state_manager.get_unused_derivation_record(1, False, True)
        our_ph = our_ph_record.puzzle_hash
        pool_ph = pool_ph_record.puzzle_hash
        api_user = WalletRpcApi(wallet_node_0)
        config = bt.config
        daemon_port = config["daemon_port"]
        test_rpc_port = find_available_listen_port("rpc_port")

        rpc_cleanup = await start_rpc_server(
            api_user,
            self_hostname,
            daemon_port,
            test_rpc_port,
            lambda x: None,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        client = await WalletRpcClient.create(self_hostname, test_rpc_port, bt.root_path, config)

        return (
            full_nodes,
            [wallet_node_0, wallet_node_1],
            [our_ph, pool_ph],
            client,  # wallet rpc client
            rpc_cleanup,
        )

    async def get_total_block_rewards(self, num_blocks):
        funds = sum(
            [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
        )
        return funds

    async def farm_blocks(self, full_node_api, ph: bytes32, num_blocks: int):
        for i in range(num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        return num_blocks
        # TODO also return calculated block rewards

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_create_new_pool_wallet_self_farm(self, one_wallet_node_and_rpc, fee, trusted):
        client, wallet_node_0, full_node_api = one_wallet_node_and_rpc
        wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )
        total_block_rewards = await self.get_total_block_rewards(PREFARMED_BLOCKS)
        await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)
        await time_out_assert(10, wallet_node_0.wallet_state_manager.blockchain.get_peak_height, PREFARMED_BLOCKS)

        our_ph = await wallet_0.get_new_puzzlehash()
        summaries_response = await client.get_wallets()
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                assert False
        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
        creation_tx: TransactionRecord = await client.create_new_pool_wallet(
            our_ph, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
        )
        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx.spend_bundle,
            creation_tx.name,
        )

        await self.farm_blocks(full_node_api, our_ph, 6)
        assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None

        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
        summaries_response = await client.get_wallets()
        wallet_id: Optional[int] = None
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                wallet_id = summary["id"]
        assert wallet_id is not None
        status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

        assert status.current.state == PoolSingletonState.SELF_POOLING.value
        assert status.target is None
        assert status.current.owner_pubkey == G1Element.from_bytes(
            bytes.fromhex(
                "b286bbf7a10fa058d2a2a758921377ef00bb7f8143e1bd40dd195ae918dbef42cfc481140f01b9eae13b430a0c8fe304"
            )
        )
        assert status.current.pool_url is None
        assert status.current.relative_lock_height == 0
        assert status.current.version == 1
        # Check that config has been written properly
        full_config: Dict = load_config(wallet_0.wallet_state_manager.root_path, "config.yaml")
        pool_list: List[Dict] = full_config["pool"]["pool_list"]
        assert len(pool_list) == 1
        pool_config = pool_list[0]
        assert (
            pool_config["owner_public_key"]
            == "0xb286bbf7a10fa058d2a2a758921377ef00bb7f8143e1bd40dd195ae918dbef42cfc481140f01b9eae13b430a0c8fe304"
        )
        # It can be one of multiple launcher IDs, due to selecting a different coin
        launcher_id = None
        for addition in creation_tx.additions:
            if addition.puzzle_hash == SINGLETON_LAUNCHER_HASH:
                launcher_id = addition.name()
                break
        assert hexstr_to_bytes(pool_config["launcher_id"]) == launcher_id
        assert pool_config["pool_url"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_create_new_pool_wallet_farm_to_pool(self, one_wallet_node_and_rpc, fee, trusted):
        client, wallet_node_0, full_node_api = one_wallet_node_and_rpc
        wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )
        total_block_rewards = await self.get_total_block_rewards(PREFARMED_BLOCKS)
        await time_out_assert(10, wallet_node_0.wallet_state_manager.blockchain.get_peak_height, PREFARMED_BLOCKS)

        await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)

        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)

        our_ph = await wallet_0.get_new_puzzlehash()
        summaries_response = await client.get_wallets()
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                assert False

        creation_tx: TransactionRecord = await client.create_new_pool_wallet(
            our_ph, "http://pool.example.com", 10, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
        )
        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx.spend_bundle,
            creation_tx.name,
        )

        await self.farm_blocks(full_node_api, our_ph, 6)
        assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None

        await time_out_assert(5, wallet_is_synced, True, wallet_node_0, full_node_api)
        summaries_response = await client.get_wallets()
        wallet_id: Optional[int] = None
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                wallet_id = summary["id"]
        assert wallet_id is not None
        status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

        assert status.current.state == PoolSingletonState.FARMING_TO_POOL.value
        assert status.target is None
        assert status.current.owner_pubkey == G1Element.from_bytes(
            bytes.fromhex(
                "b286bbf7a10fa058d2a2a758921377ef00bb7f8143e1bd40dd195ae918dbef42cfc481140f01b9eae13b430a0c8fe304"
            )
        )
        assert status.current.pool_url == "http://pool.example.com"
        assert status.current.relative_lock_height == 10
        assert status.current.version == 1
        # Check that config has been written properly
        full_config: Dict = load_config(wallet_0.wallet_state_manager.root_path, "config.yaml")
        pool_list: List[Dict] = full_config["pool"]["pool_list"]
        assert len(pool_list) == 1
        pool_config = pool_list[0]
        assert (
            pool_config["owner_public_key"]
            == "0xb286bbf7a10fa058d2a2a758921377ef00bb7f8143e1bd40dd195ae918dbef42cfc481140f01b9eae13b430a0c8fe304"
        )
        # It can be one of multiple launcher IDs, due to selecting a different coin
        launcher_id = None
        for addition in creation_tx.additions:
            if addition.puzzle_hash == SINGLETON_LAUNCHER_HASH:
                launcher_id = addition.name()
                break
        assert hexstr_to_bytes(pool_config["launcher_id"]) == launcher_id
        assert pool_config["pool_url"] == "http://pool.example.com"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_create_multiple_pool_wallets(self, one_wallet_node_and_rpc, fee, trusted):
        client, wallet_node_0, full_node_api = one_wallet_node_and_rpc
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )
        total_block_rewards = await self.get_total_block_rewards(PREFARMED_BLOCKS)
        wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
        await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)
        await time_out_assert(10, wallet_node_0.wallet_state_manager.blockchain.get_peak_height, PREFARMED_BLOCKS)
        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)

        our_ph_1 = await wallet_0.get_new_puzzlehash()
        our_ph_2 = await wallet_0.get_new_puzzlehash()
        summaries_response = await client.get_wallets()
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                assert False

        creation_tx: TransactionRecord = await client.create_new_pool_wallet(
            our_ph_1, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
        )
        creation_tx_2: TransactionRecord = await client.create_new_pool_wallet(
            our_ph_1, self_hostname, 12, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
        )

        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx.spend_bundle,
            creation_tx.name,
        )
        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx_2.spend_bundle,
            creation_tx_2.name,
        )

        await self.farm_blocks(full_node_api, our_ph_2, 6)
        assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None
        assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx_2.name) is None

        await asyncio.sleep(3)
        status_2: PoolWalletInfo = (await client.pw_status(2))[0]
        status_3: PoolWalletInfo = (await client.pw_status(3))[0]

        if status_2.current.state == PoolSingletonState.SELF_POOLING.value:
            assert status_3.current.state == PoolSingletonState.FARMING_TO_POOL.value
        else:
            assert status_2.current.state == PoolSingletonState.FARMING_TO_POOL.value
            assert status_3.current.state == PoolSingletonState.SELF_POOLING.value

        full_config: Dict = load_config(wallet_0.wallet_state_manager.root_path, "config.yaml")
        pool_list: List[Dict] = full_config["pool"]["pool_list"]
        assert len(pool_list) == 2

        assert len(await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0
        assert len(await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(3)) == 0
        # Doing a reorg reverts and removes the pool wallets
        await full_node_api.reorg_from_index_to_new_index(ReorgProtocol(uint32(0), uint32(20), our_ph_2))
        await asyncio.sleep(5)
        summaries_response = await client.get_wallets()
        assert len(summaries_response) == 1

        with pytest.raises(ValueError):
            await client.pw_status(2)
        with pytest.raises(ValueError):
            await client.pw_status(3)

        # Create some CAT wallets to increase wallet IDs
        for i in range(5):
            await asyncio.sleep(2)
            res = await client.create_new_cat_and_wallet(20)
            await asyncio.sleep(2)
            summaries_response = await client.get_wallets()
            assert res["success"]
            cat_0_id = res["wallet_id"]
            asset_id = bytes.fromhex(res["asset_id"])
            assert len(asset_id) > 0
            await self.farm_blocks(full_node_api, our_ph_2, 6)
            await time_out_assert(20, wallet_is_synced, True, wallet_node_0, full_node_api)
            bal_0 = await client.get_wallet_balance(cat_0_id)
            assert bal_0["confirmed_wallet_balance"] == 20

        # Test creation of many pool wallets. Use untrusted since that is the more complicated protocol, but don't
        # run this code more than once, since it's slow.
        if fee == 0 and not trusted:
            for i in range(22):
                await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
                creation_tx_3: TransactionRecord = await client.create_new_pool_wallet(
                    our_ph_1, self_hostname, 5, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
                )
                await time_out_assert(
                    10,
                    full_node_api.full_node.mempool_manager.get_spendbundle,
                    creation_tx_3.spend_bundle,
                    creation_tx_3.name,
                )
                await self.farm_blocks(full_node_api, our_ph_2, 2)
                await time_out_assert(20, wallet_is_synced, True, wallet_node_0, full_node_api)

                full_config: Dict = load_config(wallet_0.wallet_state_manager.root_path, "config.yaml")
                pool_list: List[Dict] = full_config["pool"]["pool_list"]
                assert len(pool_list) == i + 3
                if i == 0:
                    # Ensures that the CAT creation does not cause pool wallet IDs to increment
                    for wallet in wallet_node_0.wallet_state_manager.wallets.values():
                        if wallet.type() == WalletType.POOLING_WALLET:
                            status: PoolWalletInfo = (await client.pw_status(wallet.id()))[0]
                            assert (await wallet.get_pool_wallet_index()) < 5
                            auth_sk = find_authentication_sk(
                                [wallet_0.wallet_state_manager.private_key], status.current.owner_pubkey
                            )
                            assert auth_sk is not None
                            owner_sk = find_owner_sk(
                                [wallet_0.wallet_state_manager.private_key], status.current.owner_pubkey
                            )
                            assert owner_sk is not None
                            assert owner_sk != auth_sk

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_absorb_self(self, one_wallet_node_and_rpc, fee, trusted):
        client, wallet_node_0, full_node_api = one_wallet_node_and_rpc
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )
        wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
        total_block_rewards = await self.get_total_block_rewards(PREFARMED_BLOCKS)
        await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)
        await time_out_assert(10, wallet_node_0.wallet_state_manager.blockchain.get_peak_height, PREFARMED_BLOCKS)

        our_ph = await wallet_0.get_new_puzzlehash()
        summaries_response = await client.get_wallets()
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                assert False

        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
        creation_tx: TransactionRecord = await client.create_new_pool_wallet(
            our_ph, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
        )

        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx.spend_bundle,
            creation_tx.name,
        )
        await self.farm_blocks(full_node_api, our_ph, 1)
        await asyncio.sleep(2)
        status: PoolWalletInfo = (await client.pw_status(2))[0]

        assert status.current.state == PoolSingletonState.SELF_POOLING.value
        async with TemporaryPoolPlot(status.p2_singleton_puzzle_hash) as pool_plot:
            all_blocks = await full_node_api.get_all_full_blocks()
            blocks = bt.get_consecutive_blocks(
                3,
                block_list_input=all_blocks,
                force_plot_id=pool_plot.plot_id,
                farmer_reward_puzzle_hash=our_ph,
                guarantee_transaction_block=True,
            )

            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-3]))
            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-2]))
            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-1]))
            await asyncio.sleep(2)

            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 2 * 1750000000000

            # Claim 2 * 1.75, and farm a new 1.75
            absorb_tx: TransactionRecord = (await client.pw_absorb_rewards(2, fee))["transaction"]
            await time_out_assert(
                5,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                absorb_tx.spend_bundle,
                absorb_tx.name,
            )
            await self.farm_blocks(full_node_api, our_ph, 2)
            await asyncio.sleep(2)
            new_status: PoolWalletInfo = (await client.pw_status(2))[0]
            assert status.current == new_status.current
            assert status.tip_singleton_coin_id != new_status.tip_singleton_coin_id
            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 1 * 1750000000000

            # Claim another 1.75
            absorb_tx1: TransactionRecord = (await client.pw_absorb_rewards(2, fee))["transaction"]
            absorb_tx1.spend_bundle.debug()

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                absorb_tx1.spend_bundle,
                absorb_tx1.name,
            )

            await self.farm_blocks(full_node_api, our_ph, 2)
            await asyncio.sleep(2)
            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 0

            assert len(await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0

            tr: TransactionRecord = await client.send_transaction(
                1, 100, encode_puzzle_hash(status.p2_singleton_puzzle_hash, "tllc")
            )

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                tr.spend_bundle,
                tr.name,
            )
            await self.farm_blocks(full_node_api, our_ph, 2)
            # Balance ignores non coinbase TX
            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 0

            with pytest.raises(ValueError):
                await client.pw_absorb_rewards(2, fee)

            tx1 = await client.get_transactions(1)
            assert (250000000000 + fee) in [tx.additions[0].amount for tx in tx1]
            # await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_absorb_pooling(self, one_wallet_node_and_rpc, fee, trusted):
        client, wallet_node_0, full_node_api = one_wallet_node_and_rpc
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )
        wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
        total_block_rewards = await self.get_total_block_rewards(PREFARMED_BLOCKS)
        await time_out_assert(10, wallet_0.get_confirmed_balance, total_block_rewards)
        await time_out_assert(10, wallet_node_0.wallet_state_manager.blockchain.get_peak_height, PREFARMED_BLOCKS)

        await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
        our_ph = await wallet_0.get_new_puzzlehash()
        summaries_response = await client.get_wallets()
        for summary in summaries_response:
            if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                assert False
        # Balance stars at 6 LLC
        assert (await wallet_0.get_confirmed_balance()) == 6000000000000
        creation_tx: TransactionRecord = await client.create_new_pool_wallet(
            our_ph, "http://123.45.67.89", 10, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
        )

        await time_out_assert(
            10,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            creation_tx.spend_bundle,
            creation_tx.name,
        )
        await self.farm_blocks(full_node_api, our_ph, 1)
        await asyncio.sleep(2)
        status: PoolWalletInfo = (await client.pw_status(2))[0]

        assert status.current.state == PoolSingletonState.FARMING_TO_POOL.value
        async with TemporaryPoolPlot(status.p2_singleton_puzzle_hash) as pool_plot:
            all_blocks = await full_node_api.get_all_full_blocks()
            blocks = bt.get_consecutive_blocks(
                3,
                block_list_input=all_blocks,
                force_plot_id=pool_plot.plot_id,
                farmer_reward_puzzle_hash=our_ph,
                guarantee_transaction_block=True,
            )

            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-3]))
            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-2]))
            await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(blocks[-1]))
            await asyncio.sleep(5)
            bal = await client.get_wallet_balance(2)
            # Pooled plots don't have balance
            assert bal["confirmed_wallet_balance"] == 0

            # Claim 2 * 1.75, and farm a new 1.75
            absorb_tx: TransactionRecord = (await client.pw_absorb_rewards(2, fee))["transaction"]
            await time_out_assert(
                5,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                absorb_tx.spend_bundle,
                absorb_tx.name,
            )
            await self.farm_blocks(full_node_api, our_ph, 2)
            await asyncio.sleep(5)
            new_status: PoolWalletInfo = (await client.pw_status(2))[0]
            assert status.current == new_status.current
            assert status.tip_singleton_coin_id != new_status.tip_singleton_coin_id
            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 0

            # Claim another 1.75
            ret = await client.pw_absorb_rewards(2, fee)
            absorb_tx: TransactionRecord = ret["transaction"]
            absorb_tx.spend_bundle.debug()
            await time_out_assert(
                5,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                absorb_tx.spend_bundle,
                absorb_tx.name,
            )

            if fee == 0:
                assert ret["fee_transaction"] is None
            else:
                assert ret["fee_transaction"].fee_amount == fee
            assert absorb_tx.fee_amount == fee

            await self.farm_blocks(full_node_api, our_ph, 2)
            await asyncio.sleep(5)
            bal = await client.get_wallet_balance(2)
            assert bal["confirmed_wallet_balance"] == 0
            assert len(await wallet_node_0.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0
            assert (
                wallet_node_0.wallet_state_manager.blockchain.get_peak_height()
                == full_node_api.full_node.blockchain.get_peak().height
            )
            # Balance stars at 6 LLC and 5 more blocks are farmed, total 22 LLC
            assert (await wallet_0.get_confirmed_balance()) == 21999999999999

            num_trials = 3
            status = new_status

            await asyncio.sleep(2)
            if fee == 0:
                for i in range(num_trials):
                    all_blocks = await full_node_api.get_all_full_blocks()
                    blocks = bt.get_consecutive_blocks(
                        10,
                        block_list_input=all_blocks,
                        force_plot_id=pool_plot.plot_id,
                        farmer_reward_puzzle_hash=our_ph,
                        guarantee_transaction_block=True,
                    )
                    for block in blocks[-10:]:
                        await full_node_api.full_node.respond_block(full_node_protocol.RespondBlock(block))
                    await asyncio.sleep(2)

                    ret = await client.pw_absorb_rewards(2, fee)
                    absorb_tx: TransactionRecord = ret["transaction"]
                    await time_out_assert(
                        5,
                        full_node_api.full_node.mempool_manager.get_spendbundle,
                        absorb_tx.spend_bundle,
                        absorb_tx.name,
                    )

                    await self.farm_blocks(full_node_api, our_ph, 2)
                    await asyncio.sleep(2)
                    new_status: PoolWalletInfo = (await client.pw_status(2))[0]
                    assert status.current == new_status.current
                    assert status.tip_singleton_coin_id != new_status.tip_singleton_coin_id
                    status = new_status
                    assert ret["fee_transaction"] is None

            bal2 = await client.get_wallet_balance(2)
            assert bal2["confirmed_wallet_balance"] == 0
            # Note: as written, confirmed balance will not reflect on absorbs, because the fee
            # is paid back into the same client's wallet in this test.
            tx1 = await client.get_transactions(1)
            assert (250000000000 + fee) in [tx.additions[0].amount for tx in tx1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True])
    @pytest.mark.parametrize("fee", [0])
    async def test_self_pooling_to_pooling(self, setup, fee, trusted):
        """This tests self-pooling -> pooling"""
        num_blocks = 4  # Num blocks to farm at a time
        total_blocks = 0  # Total blocks farmed so far
        full_nodes, wallet_nodes, receive_address, client, rpc_cleanup = setup
        wallets = [wallet_n.wallet_state_manager.main_wallet for wallet_n in wallet_nodes]
        wallet_node_0 = wallet_nodes[0]
        our_ph = receive_address[0]
        pool_ph = receive_address[1]
        full_node_api = full_nodes[0]
        if trusted:
            wallet_node_0.config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_node_0.config["trusted_peers"] = {}

        await wallet_node_0.server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )

        try:
            total_blocks += await self.farm_blocks(full_node_api, our_ph, num_blocks)
            total_block_rewards = await self.get_total_block_rewards(total_blocks)

            await time_out_assert(10, wallets[0].get_unconfirmed_balance, total_block_rewards)
            await time_out_assert(10, wallets[0].get_confirmed_balance, total_block_rewards)
            await time_out_assert(10, wallets[0].get_spendable_balance, total_block_rewards)
            await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)
            assert total_block_rewards > 0

            summaries_response = await client.get_wallets()
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    assert False

            creation_tx: TransactionRecord = await client.create_new_pool_wallet(
                our_ph, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
            )
            creation_tx_2: TransactionRecord = await client.create_new_pool_wallet(
                our_ph, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
            )

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                creation_tx.spend_bundle,
                creation_tx.name,
            )
            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                creation_tx_2.spend_bundle,
                creation_tx_2.name,
            )

            await self.farm_blocks(full_node_api, our_ph, 6)
            assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None
            await time_out_assert(10, wallet_is_synced, True, wallet_node_0, full_node_api)

            summaries_response = await client.get_wallets()
            wallet_id: Optional[int] = None
            wallet_id_2: Optional[int] = None
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    if wallet_id is not None:
                        wallet_id_2 = summary["id"]
                    else:
                        wallet_id = summary["id"]
            await asyncio.sleep(1)
            assert wallet_id is not None
            assert wallet_id_2 is not None
            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
            status_2: PoolWalletInfo = (await client.pw_status(wallet_id_2))[0]

            assert status.current.state == PoolSingletonState.SELF_POOLING.value
            assert status_2.current.state == PoolSingletonState.SELF_POOLING.value
            assert status.target is None
            assert status_2.target is None

            join_pool_tx: TransactionRecord = await client.pw_join_pool(
                wallet_id,
                pool_ph,
                "https://pool.example.com",
                10,
                fee,
            )
            join_pool_tx_2: TransactionRecord = await client.pw_join_pool(
                wallet_id_2,
                pool_ph,
                "https://pool.example.com",
                10,
                fee,
            )
            assert join_pool_tx is not None
            assert join_pool_tx_2 is not None

            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
            status_2: PoolWalletInfo = (await client.pw_status(wallet_id_2))[0]

            async def tx_is_in_mempool(wid, tx: TransactionRecord):
                fetched: Optional[TransactionRecord] = await client.get_transaction(wid, tx.name)
                return fetched is not None and fetched.is_in_mempool()

            await time_out_assert(5, tx_is_in_mempool, True, wallet_id, join_pool_tx)
            await time_out_assert(5, tx_is_in_mempool, True, wallet_id_2, join_pool_tx_2)

            assert status.current.state == PoolSingletonState.SELF_POOLING.value
            assert status.target is not None
            assert status.target.state == PoolSingletonState.FARMING_TO_POOL.value
            assert status_2.current.state == PoolSingletonState.SELF_POOLING.value
            assert status_2.target is not None
            assert status_2.target.state == PoolSingletonState.FARMING_TO_POOL.value

            await self.farm_blocks(full_node_api, our_ph, 6)

            total_blocks += await self.farm_blocks(full_node_api, our_ph, num_blocks)

            async def status_is_farming_to_pool(w_id: int):
                pw_status: PoolWalletInfo = (await client.pw_status(w_id))[0]
                return pw_status.current.state == PoolSingletonState.FARMING_TO_POOL.value

            await time_out_assert(20, status_is_farming_to_pool, True, wallet_id)
            await time_out_assert(20, status_is_farming_to_pool, True, wallet_id_2)
            assert len(await wallets[0].wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0

        finally:
            client.close()
            await client.await_closed()
            await rpc_cleanup()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize(
        "fee",
        [0, FEE_AMOUNT],
    )
    async def test_leave_pool(self, setup, fee, trusted):
        """This tests self-pooling -> pooling -> escaping -> self pooling"""
        full_nodes, wallet_nodes, receive_address, client, rpc_cleanup = setup
        our_ph = receive_address[0]
        wallets = [wallet_n.wallet_state_manager.main_wallet for wallet_n in wallet_nodes]
        pool_ph = receive_address[1]
        full_node_api = full_nodes[0]
        if trusted:
            wallet_nodes[0].config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_nodes[0].config["trusted_peers"] = {}

        await wallet_nodes[0].server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )

        WAIT_SECS = 200

        try:
            summaries_response = await client.get_wallets()
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    assert False

            async def have_littlelambocoin():
                await self.farm_blocks(full_node_api, our_ph, 1)
                return (await wallets[0].get_confirmed_balance()) > 0

            await time_out_assert(timeout=WAIT_SECS, function=have_littlelambocoin)
            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            creation_tx: TransactionRecord = await client.create_new_pool_wallet(
                our_ph, "", 0, f"{self_hostname}:5000", "new", "SELF_POOLING", fee
            )

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                creation_tx.spend_bundle,
                creation_tx.name,
            )

            await self.farm_blocks(full_node_api, our_ph, 6)
            assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None

            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            summaries_response = await client.get_wallets()
            wallet_id: Optional[int] = None
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    wallet_id = summary["id"]
            assert wallet_id is not None
            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            assert status.current.state == PoolSingletonState.SELF_POOLING.value
            assert status.target is None

            join_pool_tx: TransactionRecord = await client.pw_join_pool(
                wallet_id,
                pool_ph,
                "https://pool.example.com",
                5,
                fee,
            )
            assert join_pool_tx is not None

            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            assert status.current.state == PoolSingletonState.SELF_POOLING.value
            assert status.current.pool_url is None
            assert status.current.relative_lock_height == 0
            assert status.current.state == 1
            assert status.current.version == 1

            assert status.target
            assert status.target.pool_url == "https://pool.example.com"
            assert status.target.relative_lock_height == 5
            assert status.target.state == 3
            assert status.target.version == 1

            async def status_is_farming_to_pool():
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.FARMING_TO_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_farming_to_pool)

            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            leave_pool_tx: TransactionRecord = await client.pw_self_pool(wallet_id, fee)
            assert leave_pool_tx.wallet_id == wallet_id
            assert leave_pool_tx.amount == 1

            async def status_is_leaving():
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.LEAVING_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_leaving)

            async def status_is_self_pooling():
                # Farm enough blocks to wait for relative_lock_height
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.SELF_POOLING.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_self_pooling)
            assert len(await wallets[0].wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0

        finally:
            client.close()
            await client.await_closed()
            await rpc_cleanup()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_change_pools(self, setup, fee, trusted):
        """This tests Pool A -> escaping -> Pool B"""
        full_nodes, wallet_nodes, receive_address, client, rpc_cleanup = setup
        our_ph = receive_address[0]
        pool_a_ph = receive_address[1]
        wallets = [wallet_n.wallet_state_manager.main_wallet for wallet_n in wallet_nodes]
        pool_b_ph = await wallets[1].get_new_puzzlehash()
        full_node_api = full_nodes[0]

        if trusted:
            wallet_nodes[0].config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_nodes[0].config["trusted_peers"] = {}

        await wallet_nodes[0].server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )

        WAIT_SECS = 200
        try:
            summaries_response = await client.get_wallets()
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    assert False

            async def have_littlelambocoin():
                await self.farm_blocks(full_node_api, our_ph, 1)
                return (await wallets[0].get_confirmed_balance()) > 0

            await time_out_assert(timeout=WAIT_SECS, function=have_littlelambocoin)
            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            creation_tx: TransactionRecord = await client.create_new_pool_wallet(
                pool_a_ph, "https://pool-a.org", 5, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
            )

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                creation_tx.spend_bundle,
                creation_tx.name,
            )

            await self.farm_blocks(full_node_api, our_ph, 6)
            assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None

            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            summaries_response = await client.get_wallets()
            wallet_id: Optional[int] = None
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    wallet_id = summary["id"]
            assert wallet_id is not None
            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            assert status.current.state == PoolSingletonState.FARMING_TO_POOL.value
            assert status.target is None

            async def status_is_farming_to_pool():
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.FARMING_TO_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_farming_to_pool)

            pw_info: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
            assert pw_info.current.pool_url == "https://pool-a.org"
            assert pw_info.current.relative_lock_height == 5
            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            join_pool_tx: TransactionRecord = await client.pw_join_pool(
                wallet_id,
                pool_b_ph,
                "https://pool-b.org",
                10,
                fee,
            )
            assert join_pool_tx is not None

            async def status_is_leaving():
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.LEAVING_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_leaving)
            pw_info: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            await time_out_assert(timeout=WAIT_SECS, function=status_is_farming_to_pool)
            pw_info: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
            assert pw_info.current.pool_url == "https://pool-b.org"
            assert pw_info.current.relative_lock_height == 10
            assert len(await wallets[0].wallet_state_manager.tx_store.get_unconfirmed_for_wallet(2)) == 0

        finally:
            client.close()
            await client.await_closed()
            await rpc_cleanup()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trusted", [True, False])
    @pytest.mark.parametrize("fee", [0, FEE_AMOUNT])
    async def test_change_pools_reorg(self, setup, fee, trusted):
        """This tests Pool A -> escaping -> reorg -> escaping -> Pool B"""
        full_nodes, wallet_nodes, receive_address, client, rpc_cleanup = setup
        our_ph = receive_address[0]
        pool_a_ph = receive_address[1]
        wallets = [wallet_n.wallet_state_manager.main_wallet for wallet_n in wallet_nodes]
        pool_b_ph = await wallets[1].get_new_puzzlehash()
        full_node_api = full_nodes[0]
        WAIT_SECS = 30
        if trusted:
            wallet_nodes[0].config["trusted_peers"] = {
                full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
            }
        else:
            wallet_nodes[0].config["trusted_peers"] = {}

        await wallet_nodes[0].server.start_client(
            PeerInfo(self_hostname, uint16(full_node_api.full_node.server._port)), None
        )

        try:
            summaries_response = await client.get_wallets()
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    assert False

            async def have_littlelambocoin():
                await self.farm_blocks(full_node_api, our_ph, 1)
                return (await wallets[0].get_confirmed_balance()) > 0

            await time_out_assert(timeout=WAIT_SECS, function=have_littlelambocoin)
            await time_out_assert(10, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            creation_tx: TransactionRecord = await client.create_new_pool_wallet(
                pool_a_ph, "https://pool-a.org", 5, f"{self_hostname}:5000", "new", "FARMING_TO_POOL", fee
            )

            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                creation_tx.spend_bundle,
                creation_tx.name,
            )

            await self.farm_blocks(full_node_api, our_ph, 6)
            assert full_node_api.full_node.mempool_manager.get_spendbundle(creation_tx.name) is None

            await time_out_assert(5, wallet_is_synced, True, wallet_nodes[0], full_node_api)

            summaries_response = await client.get_wallets()
            wallet_id: Optional[int] = None
            for summary in summaries_response:
                if WalletType(int(summary["type"])) == WalletType.POOLING_WALLET:
                    wallet_id = summary["id"]
            assert wallet_id is not None
            status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]

            assert status.current.state == PoolSingletonState.FARMING_TO_POOL.value
            assert status.target is None

            async def status_is_farming_to_pool():
                await self.farm_blocks(full_node_api, our_ph, 1)
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.FARMING_TO_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_farming_to_pool)

            pw_info: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
            assert pw_info.current.pool_url == "https://pool-a.org"
            assert pw_info.current.relative_lock_height == 5

            join_pool_tx: TransactionRecord = await client.pw_join_pool(
                wallet_id,
                pool_b_ph,
                "https://pool-b.org",
                10,
                fee,
            )
            assert join_pool_tx is not None
            await time_out_assert(
                10,
                full_node_api.full_node.mempool_manager.get_spendbundle,
                join_pool_tx.spend_bundle,
                join_pool_tx.name,
            )
            await self.farm_blocks(full_node_api, our_ph, 1)

            async def status_is_leaving_no_blocks():
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.LEAVING_POOL.value

            async def status_is_farming_to_pool_no_blocks():
                pw_status: PoolWalletInfo = (await client.pw_status(wallet_id))[0]
                return pw_status.current.state == PoolSingletonState.FARMING_TO_POOL.value

            await time_out_assert(timeout=WAIT_SECS, function=status_is_leaving_no_blocks)

            current_blocks = await full_node_api.get_all_full_blocks()
            more_blocks = full_node_api.bt.get_consecutive_blocks(
                3,
                farmer_reward_puzzle_hash=pool_a_ph,
                pool_reward_puzzle_hash=pool_b_ph,
                block_list_input=current_blocks[:-1],
                force_overflow=True,
                guarantee_transaction_block=True,
                seed=32 * b"4",
                transaction_data=join_pool_tx.spend_bundle,
            )

            for block in more_blocks[-3:]:
                await full_node_api.full_node.respond_block(RespondBlock(block))

            await asyncio.sleep(5)
            await time_out_assert(timeout=WAIT_SECS, function=status_is_leaving_no_blocks)

            # Eventually, leaves pool
            await time_out_assert(timeout=WAIT_SECS, function=status_is_farming_to_pool)

        finally:
            client.close()
            await client.await_closed()
            await rpc_cleanup()
