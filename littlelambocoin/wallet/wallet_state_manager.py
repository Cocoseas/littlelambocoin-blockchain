import asyncio
import json
import logging
import multiprocessing
import multiprocessing.context
import time
import traceback
from collections import defaultdict
from pathlib import Path
from secrets import token_bytes
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiosqlite
from blspy import G1Element, PrivateKey

from littlelambocoin.consensus.coinbase import pool_parent_id, farmer_parent_id
from littlelambocoin.consensus.constants import ConsensusConstants
from littlelambocoin.pools.pool_puzzles import SINGLETON_LAUNCHER_HASH, solution_to_pool_state
from littlelambocoin.pools.pool_wallet import PoolWallet
from littlelambocoin.protocols import wallet_protocol
from littlelambocoin.protocols.wallet_protocol import PuzzleSolutionResponse, RespondPuzzleSolution, CoinState
from littlelambocoin.server.ws_connection import WSLittlelambocoinConnection
from littlelambocoin.types.blockchain_format.coin import Coin
from littlelambocoin.types.blockchain_format.program import Program
from littlelambocoin.types.blockchain_format.sized_bytes import bytes32
from littlelambocoin.types.coin_spend import CoinSpend
from littlelambocoin.types.full_block import FullBlock
from littlelambocoin.types.mempool_inclusion_status import MempoolInclusionStatus
from littlelambocoin.util.byte_types import hexstr_to_bytes
from littlelambocoin.util.config import process_config_start_method
from littlelambocoin.util.db_wrapper import DBWrapper
from littlelambocoin.util.errors import Err
from littlelambocoin.util.ints import uint32, uint64, uint128, uint8
from littlelambocoin.util.db_synchronous import db_synchronous_on
from littlelambocoin.wallet.cat_wallet.cat_utils import match_cat_puzzle, construct_cat_puzzle
from littlelambocoin.wallet.cat_wallet.cat_wallet import CATWallet
from littlelambocoin.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from littlelambocoin.wallet.derivation_record import DerivationRecord
from littlelambocoin.wallet.derive_keys import master_sk_to_wallet_sk, master_sk_to_wallet_sk_unhardened
from littlelambocoin.wallet.key_val_store import KeyValStore
from littlelambocoin.wallet.puzzles.cat_loader import CAT_MOD
from littlelambocoin.wallet.rl_wallet.rl_wallet import RLWallet
from littlelambocoin.wallet.settings.user_settings import UserSettings
from littlelambocoin.wallet.trade_manager import TradeManager
from littlelambocoin.wallet.transaction_record import TransactionRecord
from littlelambocoin.wallet.util.compute_hints import compute_coin_hints
from littlelambocoin.wallet.util.transaction_type import TransactionType
from littlelambocoin.wallet.util.wallet_sync_utils import last_change_height_cs
from littlelambocoin.wallet.util.wallet_types import WalletType
from littlelambocoin.wallet.wallet import Wallet
from littlelambocoin.wallet.wallet_action import WalletAction
from littlelambocoin.wallet.wallet_action_store import WalletActionStore
from littlelambocoin.wallet.wallet_blockchain import WalletBlockchain
from littlelambocoin.wallet.wallet_coin_record import WalletCoinRecord
from littlelambocoin.wallet.wallet_coin_store import WalletCoinStore
from littlelambocoin.wallet.wallet_info import WalletInfo
from littlelambocoin.wallet.wallet_interested_store import WalletInterestedStore
from littlelambocoin.wallet.wallet_pool_store import WalletPoolStore
from littlelambocoin.wallet.wallet_puzzle_store import WalletPuzzleStore
from littlelambocoin.wallet.wallet_sync_store import WalletSyncStore
from littlelambocoin.wallet.wallet_transaction_store import WalletTransactionStore
from littlelambocoin.wallet.wallet_user_store import WalletUserStore
from littlelambocoin.server.server import LittlelambocoinServer
from littlelambocoin.wallet.did_wallet.did_wallet import DIDWallet
from littlelambocoin.wallet.wallet_weight_proof_handler import WalletWeightProofHandler


class WalletStateManager:
    constants: ConsensusConstants
    config: Dict
    tx_store: WalletTransactionStore
    puzzle_store: WalletPuzzleStore
    user_store: WalletUserStore
    action_store: WalletActionStore
    basic_store: KeyValStore

    start_index: int

    # Makes sure only one asyncio thread is changing the blockchain state at one time
    lock: asyncio.Lock

    log: logging.Logger

    # TODO Don't allow user to send tx until wallet is synced
    sync_mode: bool
    sync_target: uint32
    genesis: FullBlock

    state_changed_callback: Optional[Callable]
    pending_tx_callback: Optional[Callable]
    puzzle_hash_created_callbacks: Dict = defaultdict(lambda *x: None)
    db_path: Path
    db_connection: aiosqlite.Connection
    db_wrapper: DBWrapper

    main_wallet: Wallet
    wallets: Dict[uint32, Any]
    private_key: PrivateKey

    trade_manager: TradeManager
    new_wallet: bool
    user_settings: UserSettings
    blockchain: WalletBlockchain
    coin_store: WalletCoinStore
    sync_store: WalletSyncStore
    finished_sync_up_to: uint32
    interested_store: WalletInterestedStore
    multiprocessing_context: multiprocessing.context.BaseContext
    weight_proof_handler: WalletWeightProofHandler
    server: LittlelambocoinServer
    root_path: Path
    wallet_node: Any
    pool_store: WalletPoolStore
    default_cats: Dict[str, Any]

    @staticmethod
    async def create(
        private_key: PrivateKey,
        config: Dict,
        db_path: Path,
        constants: ConsensusConstants,
        server: LittlelambocoinServer,
        root_path: Path,
        wallet_node,
        name: str = None,
    ):
        self = WalletStateManager()
        self.new_wallet = False
        self.config = config
        self.constants = constants
        self.server = server
        self.root_path = root_path
        self.log = logging.getLogger(name if name else __name__)
        self.lock = asyncio.Lock()
        self.log.debug(f"Starting in db path: {db_path}")
        self.db_connection = await aiosqlite.connect(db_path)
        await self.db_connection.execute("pragma journal_mode=wal")

        await self.db_connection.execute(
            "pragma synchronous={}".format(db_synchronous_on(self.config.get("db_sync", "auto"), db_path))
        )

        self.db_wrapper = DBWrapper(self.db_connection)
        self.coin_store = await WalletCoinStore.create(self.db_wrapper)
        self.tx_store = await WalletTransactionStore.create(self.db_wrapper)
        self.puzzle_store = await WalletPuzzleStore.create(self.db_wrapper)
        self.user_store = await WalletUserStore.create(self.db_wrapper)
        self.action_store = await WalletActionStore.create(self.db_wrapper)
        self.basic_store = await KeyValStore.create(self.db_wrapper)
        self.trade_manager = await TradeManager.create(self, self.db_wrapper)
        self.user_settings = await UserSettings.create(self.basic_store)
        self.pool_store = await WalletPoolStore.create(self.db_wrapper)
        self.interested_store = await WalletInterestedStore.create(self.db_wrapper)
        self.default_cats = DEFAULT_CATS

        self.wallet_node = wallet_node
        self.sync_mode = False
        self.sync_target = uint32(0)
        self.finished_sync_up_to = uint32(0)
        multiprocessing_start_method = process_config_start_method(config=self.config, log=self.log)
        self.multiprocessing_context = multiprocessing.get_context(method=multiprocessing_start_method)
        self.weight_proof_handler = WalletWeightProofHandler(
            constants=self.constants,
            multiprocessing_context=self.multiprocessing_context,
        )
        self.blockchain = await WalletBlockchain.create(self.basic_store, self.constants, self.weight_proof_handler)

        self.state_changed_callback = None
        self.pending_tx_callback = None
        self.db_path = db_path

        main_wallet_info = await self.user_store.get_wallet_by_id(1)
        assert main_wallet_info is not None

        self.private_key = private_key
        self.main_wallet = await Wallet.create(self, main_wallet_info)

        self.wallets = {main_wallet_info.id: self.main_wallet}

        wallet = None
        for wallet_info in await self.get_all_wallet_info_entries():
            if wallet_info.type == WalletType.STANDARD_WALLET:
                if wallet_info.id == 1:
                    continue
                wallet = await Wallet.create(config, wallet_info)
            elif wallet_info.type == WalletType.CAT:
                wallet = await CATWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            elif wallet_info.type == WalletType.RATE_LIMITED:
                wallet = await RLWallet.create(self, wallet_info)
            elif wallet_info.type == WalletType.DISTRIBUTED_ID:
                wallet = await DIDWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            elif wallet_info.type == WalletType.POOLING_WALLET:
                wallet = await PoolWallet.create_from_db(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
            if wallet is not None:
                self.wallets[wallet_info.id] = wallet

        return self

    def get_derivation_index(self, pubkey: G1Element, max_depth: int = 1000) -> int:
        for i in range(0, max_depth):
            derived = self.get_public_key(uint32(i))
            if derived == pubkey:
                return i
            derived = self.get_public_key_unhardened(uint32(i))
            if derived == pubkey:
                return i
        return -1

    def get_public_key(self, index: uint32) -> G1Element:
        return master_sk_to_wallet_sk(self.private_key, index).get_g1()

    def get_public_key_unhardened(self, index: uint32) -> G1Element:
        return master_sk_to_wallet_sk_unhardened(self.private_key, index).get_g1()

    async def load_wallets(self):
        for wallet_info in await self.get_all_wallet_info_entries():
            if wallet_info.id in self.wallets:
                continue
            if wallet_info.type == WalletType.STANDARD_WALLET:
                if wallet_info.id == 1:
                    continue
                wallet = await Wallet.create(self.config, wallet_info)
                self.wallets[wallet_info.id] = wallet
            # TODO add RL AND DiD WALLETS HERE
            elif wallet_info.type == WalletType.CAT:
                wallet = await CATWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
                self.wallets[wallet_info.id] = wallet
            elif wallet_info.type == WalletType.DISTRIBUTED_ID:
                wallet = await DIDWallet.create(
                    self,
                    self.main_wallet,
                    wallet_info,
                )
                self.wallets[wallet_info.id] = wallet

    async def get_keys(self, puzzle_hash: bytes32) -> Optional[Tuple[G1Element, PrivateKey]]:
        record = await self.puzzle_store.record_for_puzzle_hash(puzzle_hash)
        if record is None:
            raise ValueError(f"No key for this puzzlehash {puzzle_hash})")
        if record.hardened:
            private = master_sk_to_wallet_sk(self.private_key, record.index)
            pubkey = private.get_g1()
            return pubkey, private
        private = master_sk_to_wallet_sk_unhardened(self.private_key, record.index)
        pubkey = private.get_g1()
        return pubkey, private

    async def create_more_puzzle_hashes(self, from_zero: bool = False, in_transaction=False):
        """
        For all wallets in the user store, generates the first few puzzle hashes so
        that we can restore the wallet from only the private keys.
        """
        targets = list(self.wallets.keys())

        unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
        if unused is None:
            # This handles the case where the database has entries but they have all been used
            unused = await self.puzzle_store.get_last_derivation_path()
            if unused is None:
                # This handles the case where the database is empty
                unused = uint32(0)

        to_generate = self.config["initial_num_public_keys"]

        for wallet_id in targets:
            target_wallet = self.wallets[wallet_id]

            last: Optional[uint32] = await self.puzzle_store.get_last_derivation_path_for_wallet(wallet_id)

            start_index = 0
            derivation_paths: List[DerivationRecord] = []

            if last is not None:
                start_index = last + 1

            # If the key was replaced (from_zero=True), we should generate the puzzle hashes for the new key
            if from_zero:
                start_index = 0

            for index in range(start_index, unused + to_generate):
                if WalletType(target_wallet.type()) == WalletType.POOLING_WALLET:
                    continue

                # Hardened
                pubkey: G1Element = self.get_public_key(uint32(index))
                puzzle: Program = target_wallet.puzzle_for_pk(bytes(pubkey))
                if puzzle is None:
                    self.log.error(f"Unable to create puzzles with wallet {target_wallet}")
                    break
                puzzlehash: bytes32 = puzzle.get_tree_hash()
                self.log.info(f"Puzzle at index {index} wallet ID {wallet_id} puzzle hash {puzzlehash.hex()}")
                derivation_paths.append(
                    DerivationRecord(
                        uint32(index), puzzlehash, pubkey, target_wallet.type(), uint32(target_wallet.id()), True
                    )
                )
                # Unhardened
                pubkey_unhardened: G1Element = self.get_public_key_unhardened(uint32(index))
                puzzle_unhardened: Program = target_wallet.puzzle_for_pk(bytes(pubkey_unhardened))
                if puzzle_unhardened is None:
                    self.log.error(f"Unable to create puzzles with wallet {target_wallet}")
                    break
                puzzlehash_unhardened: bytes32 = puzzle_unhardened.get_tree_hash()
                self.log.info(
                    f"Puzzle at index {index} wallet ID {wallet_id} puzzle hash {puzzlehash_unhardened.hex()}"
                )
                derivation_paths.append(
                    DerivationRecord(
                        uint32(index),
                        puzzlehash_unhardened,
                        pubkey_unhardened,
                        target_wallet.type(),
                        uint32(target_wallet.id()),
                        False,
                    )
                )
            await self.puzzle_store.add_derivation_paths(derivation_paths, in_transaction)
            await self.add_interested_puzzle_hashes(
                [record.puzzle_hash for record in derivation_paths],
                [record.wallet_id for record in derivation_paths],
                in_transaction,
            )
        if unused > 0:
            await self.puzzle_store.set_used_up_to(uint32(unused - 1), in_transaction)

    async def update_wallet_puzzle_hashes(self, wallet_id):
        derivation_paths: List[DerivationRecord] = []
        target_wallet = self.wallets[wallet_id]
        last: Optional[uint32] = await self.puzzle_store.get_last_derivation_path_for_wallet(wallet_id)
        unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
        if unused is None:
            # This handles the case where the database has entries but they have all been used
            unused = await self.puzzle_store.get_last_derivation_path()
            if unused is None:
                # This handles the case where the database is empty
                unused = uint32(0)
        for index in range(unused, last):
            # Since DID are not released yet we can assume they are only using unhardened keys derivation
            pubkey: G1Element = self.get_public_key_unhardened(uint32(index))
            puzzle: Program = target_wallet.puzzle_for_pk(bytes(pubkey))
            puzzlehash: bytes32 = puzzle.get_tree_hash()
            self.log.info(f"Generating public key at index {index} puzzle hash {puzzlehash.hex()}")
            derivation_paths.append(
                DerivationRecord(
                    uint32(index),
                    puzzlehash,
                    pubkey,
                    target_wallet.wallet_info.type,
                    uint32(target_wallet.wallet_info.id),
                    False,
                )
            )
        await self.puzzle_store.add_derivation_paths(derivation_paths)

    async def get_unused_derivation_record(
        self, wallet_id: uint32, in_transaction=False, hardened=False
    ) -> DerivationRecord:
        """
        Creates a puzzle hash for the given wallet, and then makes more puzzle hashes
        for every wallet to ensure we always have more in the database. Never reusue the
        same public key more than once (for privacy).
        """
        async with self.puzzle_store.lock:
            # If we have no unused public keys, we will create new ones
            unused: Optional[uint32] = await self.puzzle_store.get_unused_derivation_path()
            if unused is None:
                await self.create_more_puzzle_hashes()

            # Now we must have unused public keys
            unused = await self.puzzle_store.get_unused_derivation_path()
            assert unused is not None
            record: Optional[DerivationRecord] = await self.puzzle_store.get_derivation_record(
                unused, wallet_id, hardened
            )
            assert record is not None

            # Set this key to used so we never use it again
            await self.puzzle_store.set_used_up_to(record.index, in_transaction=in_transaction)

            # Create more puzzle hashes / keys
            await self.create_more_puzzle_hashes(in_transaction=in_transaction)
            return record

    async def get_current_derivation_record_for_wallet(self, wallet_id: uint32) -> Optional[DerivationRecord]:
        async with self.puzzle_store.lock:
            # If we have no unused public keys, we will create new ones
            current: Optional[DerivationRecord] = await self.puzzle_store.get_current_derivation_record_for_wallet(
                wallet_id
            )
            return current

    def set_callback(self, callback: Callable):
        """
        Callback to be called when the state of the wallet changes.
        """
        self.state_changed_callback = callback

    def set_pending_callback(self, callback: Callable):
        """
        Callback to be called when new pending transaction enters the store
        """
        self.pending_tx_callback = callback

    def set_coin_with_puzzlehash_created_callback(self, puzzlehash: bytes32, callback: Callable):
        """
        Callback to be called when new coin is seen with specified puzzlehash
        """
        self.puzzle_hash_created_callbacks[puzzlehash] = callback

    async def puzzle_hash_created(self, coin: Coin):
        callback = self.puzzle_hash_created_callbacks[coin.puzzle_hash]
        if callback is None:
            return None
        await callback(coin)

    def state_changed(self, state: str, wallet_id: int = None, data_object=None):
        """
        Calls the callback if it's present.
        """
        if data_object is None:
            data_object = {}
        if self.state_changed_callback is None:
            return None
        self.state_changed_callback(state, wallet_id, data_object)

    def tx_pending_changed(self) -> None:
        """
        Notifies the wallet node that there's new tx pending
        """
        if self.pending_tx_callback is None:
            return None

        self.pending_tx_callback()

    async def synced(self):
        latest = await self.blockchain.get_peak_block()
        if latest is None:
            return False

        if latest.height - await self.blockchain.get_finished_sync_up_to() > 1:
            return False

        latest_timestamp = self.blockchain.get_latest_timestamp()

        if latest_timestamp > int(time.time()) - 10 * 60:
            return True
        return False

    def set_sync_mode(self, mode: bool, sync_height: uint32 = uint32(0)):
        """
        Sets the sync mode. This changes the behavior of the wallet node.
        """
        self.sync_mode = mode
        self.sync_target = sync_height
        self.state_changed("sync_changed")

    async def get_confirmed_spendable_balance_for_wallet(self, wallet_id: int, unspent_records=None) -> uint128:
        """
        Returns the balance amount of all coins that are spendable.
        """

        spendable: Set[WalletCoinRecord] = await self.get_spendable_coins_for_wallet(wallet_id, unspent_records)

        spendable_amount: uint128 = uint128(0)
        for record in spendable:
            spendable_amount = uint128(spendable_amount + record.coin.amount)

        return spendable_amount

    async def does_coin_belong_to_wallet(self, coin: Coin, wallet_id: int) -> bool:
        """
        Returns true if we have the key for this coin.
        """
        info = await self.puzzle_store.wallet_info_for_puzzle_hash(coin.puzzle_hash)

        if info is None:
            return False

        coin_wallet_id, wallet_type = info
        if wallet_id == coin_wallet_id:
            return True

        return False

    async def get_confirmed_balance_for_wallet(
        self,
        wallet_id: int,
        unspent_coin_records: Optional[Set[WalletCoinRecord]] = None,
    ) -> uint128:
        """
        Returns the confirmed balance, including coinbase rewards that are not spendable.
        """
        # lock only if unspent_coin_records is None
        if unspent_coin_records is None:
            unspent_coin_records = await self.coin_store.get_unspent_coins_for_wallet(wallet_id)
        return uint128(sum(cr.coin.amount for cr in unspent_coin_records))

    async def get_unconfirmed_balance(
        self, wallet_id: int, unspent_coin_records: Optional[Set[WalletCoinRecord]] = None
    ) -> uint128:
        """
        Returns the balance, including coinbase rewards that are not spendable, and unconfirmed
        transactions.
        """
        # This API should change so that get_balance_from_coin_records is called for Set[WalletCoinRecord]
        # and this method is called only for the unspent_coin_records==None case.
        if unspent_coin_records is None:
            unspent_coin_records = await self.coin_store.get_unspent_coins_for_wallet(wallet_id)

        unconfirmed_tx: List[TransactionRecord] = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        all_unspent_coins: Set[Coin] = {cr.coin for cr in unspent_coin_records}

        for record in unconfirmed_tx:
            for addition in record.additions:
                # This change or a self transaction
                if await self.does_coin_belong_to_wallet(addition, wallet_id):
                    all_unspent_coins.add(addition)

            for removal in record.removals:
                if await self.does_coin_belong_to_wallet(removal, wallet_id) and removal in all_unspent_coins:
                    all_unspent_coins.remove(removal)

        return uint128(sum(coin.amount for coin in all_unspent_coins))

    async def unconfirmed_removals_for_wallet(self, wallet_id: int) -> Dict[bytes32, Coin]:
        """
        Returns new removals transactions that have not been confirmed yet.
        """
        removals: Dict[bytes32, Coin] = {}
        unconfirmed_tx = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        for record in unconfirmed_tx:
            for coin in record.removals:
                removals[coin.name()] = coin
        return removals

    async def fetch_parent_and_check_for_cat(
        self, peer: WSLittlelambocoinConnection, coin_state: CoinState, fork_height: Optional[uint32]
    ) -> Tuple[Optional[uint32], Optional[WalletType]]:
        if self.is_pool_reward(coin_state.created_height, coin_state.coin.parent_coin_info) or self.is_farmer_reward(
            coin_state.created_height, coin_state.coin.parent_coin_info
        ):
            return None, None

        response: List[CoinState] = await self.wallet_node.get_coin_state(
            [coin_state.coin.parent_coin_info], fork_height, peer
        )
        if len(response) == 0:
            self.log.warning(f"Could not find a parent coin with ID: {coin_state.coin.parent_coin_info}")
            return None, None
        parent_coin_state = response[0]
        assert parent_coin_state.spent_height == coin_state.created_height
        wallet_id = None
        wallet_type = None
        cs: Optional[CoinSpend] = await self.wallet_node.fetch_puzzle_solution(
            peer, parent_coin_state.spent_height, parent_coin_state.coin
        )
        if cs is None:
            return None, None
        matched, curried_args = match_cat_puzzle(Program.from_bytes(bytes(cs.puzzle_reveal)))

        if matched:
            mod_hash, tail_hash, inner_puzzle = curried_args
            inner_puzzle_hash = inner_puzzle.get_tree_hash()
            self.log.info(
                f"parent: {parent_coin_state.coin.name()} inner_puzzle_hash for parent is {inner_puzzle_hash}"
            )

            hint_list = compute_coin_hints(cs)
            derivation_record = None
            for hint in hint_list:
                derivation_record = await self.puzzle_store.get_derivation_record_for_puzzle_hash(bytes32(hint))
                if derivation_record is not None:
                    break

            if derivation_record is None:
                self.log.info(f"Received state for the coin that doesn't belong to us {coin_state}")
            else:
                our_inner_puzzle: Program = self.main_wallet.puzzle_for_pk(bytes(derivation_record.pubkey))
                cat_puzzle = construct_cat_puzzle(CAT_MOD, bytes32(bytes(tail_hash)[1:]), our_inner_puzzle)
                if cat_puzzle.get_tree_hash() != coin_state.coin.puzzle_hash:
                    return None, None
                if bytes(tail_hash).hex()[2:] in self.default_cats or self.config.get(
                    "automatically_add_unknown_cats", False
                ):
                    cat_wallet = await CATWallet.create_wallet_for_cat(
                        self, self.main_wallet, bytes(tail_hash).hex()[2:], in_transaction=True
                    )
                    wallet_id = cat_wallet.id()
                    wallet_type = WalletType(cat_wallet.type())
                    self.state_changed("wallet_created")

        return wallet_id, wallet_type

    async def new_coin_state(
        self, coin_states: List[CoinState], peer: WSLittlelambocoinConnection, fork_height: Optional[uint32]
    ) -> None:
        # TODO: add comment about what this method does

        # Input states should already be sorted by cs_height, with reorgs at the beginning
        curr_h = -1
        for c_state in coin_states:
            last_change_height = last_change_height_cs(c_state)
            if last_change_height < curr_h:
                raise ValueError("Input coin_states is not sorted properly")
            curr_h = last_change_height

        all_txs_per_wallet: Dict[int, List[TransactionRecord]] = {}
        trade_removals = await self.trade_manager.get_coins_of_interest()
        all_unconfirmed: List[TransactionRecord] = await self.tx_store.get_all_unconfirmed()
        trade_coin_removed: List[CoinState] = []

        for coin_state_idx, coin_state in enumerate(coin_states):
            wallet_info: Optional[Tuple[uint32, WalletType]] = await self.get_wallet_id_for_puzzle_hash(
                coin_state.coin.puzzle_hash
            )
            local_record: Optional[WalletCoinRecord] = await self.coin_store.get_coin_record(coin_state.coin.name())
            self.log.debug(f"{coin_state.coin.name()}: {coin_state}")

            # If we already have this coin, and it was spent and confirmed at the same heights, then we return (done)
            if local_record is not None:
                local_spent = None
                if local_record.spent_block_height != 0:
                    local_spent = local_record.spent_block_height
                if (
                    local_spent == coin_state.spent_height
                    and local_record.confirmed_block_height == coin_state.created_height
                ):
                    continue

            wallet_id: Optional[uint32] = None
            wallet_type: Optional[WalletType] = None
            if wallet_info is not None:
                wallet_id, wallet_type = wallet_info
            elif local_record is not None:
                wallet_id = uint32(local_record.wallet_id)
                wallet_type = local_record.wallet_type
            elif coin_state.created_height is not None:
                wallet_id, wallet_type = await self.fetch_parent_and_check_for_cat(peer, coin_state, fork_height)

            if wallet_id is None or wallet_type is None:
                self.log.info(f"No wallet for coin state: {coin_state}")
                continue

            if wallet_id in all_txs_per_wallet:
                all_txs = all_txs_per_wallet[wallet_id]
            else:
                all_txs = await self.tx_store.get_all_transactions_for_wallet(wallet_id)
                all_txs_per_wallet[wallet_id] = all_txs

            all_outgoing = [tx for tx in all_txs if "OUTGOING" in TransactionType(tx.type).name]

            derivation_index = await self.puzzle_store.index_for_puzzle_hash(coin_state.coin.puzzle_hash)
            if derivation_index is not None:
                await self.puzzle_store.set_used_up_to(derivation_index, True)

            if coin_state.created_height is None:
                # TODO implements this coin got reorged
                # TODO: we need to potentially roll back the pool wallet here
                pass
            elif coin_state.created_height is not None and coin_state.spent_height is None:
                await self.coin_added(coin_state.coin, coin_state.created_height, all_txs, wallet_id, wallet_type)
            elif coin_state.created_height is not None and coin_state.spent_height is not None:
                self.log.info(f"Coin Removed: {coin_state}")
                record = await self.coin_store.get_coin_record(coin_state.coin.name())
                if coin_state.coin.name() in trade_removals:
                    trade_coin_removed.append(coin_state)
                children: Optional[List[CoinState]] = None
                if record is None:
                    farmer_reward = False
                    pool_reward = False
                    tx_type: int
                    if self.is_farmer_reward(coin_state.created_height, coin_state.coin.parent_coin_info):
                        farmer_reward = True
                        tx_type = TransactionType.FEE_REWARD.value
                    elif self.is_pool_reward(coin_state.created_height, coin_state.coin.parent_coin_info):
                        pool_reward = True
                        tx_type = TransactionType.COINBASE_REWARD.value
                    else:
                        tx_type = TransactionType.INCOMING_TX.value
                    record = WalletCoinRecord(
                        coin_state.coin,
                        coin_state.created_height,
                        coin_state.spent_height,
                        True,
                        farmer_reward or pool_reward,
                        wallet_type,
                        wallet_id,
                    )
                    await self.coin_store.add_coin_record(record)
                    # Coin first received
                    coin_record: Optional[WalletCoinRecord] = await self.coin_store.get_coin_record(
                        coin_state.coin.parent_coin_info
                    )
                    if coin_record is not None and wallet_type.value == coin_record.wallet_type:
                        change = True
                    else:
                        change = False

                    if not change:
                        created_timestamp = await self.wallet_node.get_timestamp_for_height(coin_state.created_height)
                        tx_record = TransactionRecord(
                            confirmed_at_height=coin_state.created_height,
                            created_at_time=uint64(created_timestamp),
                            to_puzzle_hash=(await self.convert_puzzle_hash(wallet_id, coin_state.coin.puzzle_hash)),
                            amount=uint64(coin_state.coin.amount),
                            fee_amount=uint64(0),
                            confirmed=True,
                            sent=uint32(0),
                            spend_bundle=None,
                            additions=[coin_state.coin],
                            removals=[],
                            wallet_id=wallet_id,
                            sent_to=[],
                            trade_id=None,
                            type=uint32(tx_type),
                            name=bytes32(token_bytes()),
                            memos=[],
                        )
                        await self.tx_store.add_transaction_record(tx_record, True)

                    children = await self.wallet_node.fetch_children(peer, coin_state.coin.name(), fork_height)
                    assert children is not None
                    additions = [state.coin for state in children]
                    if len(children) > 0:
                        fee = 0

                        to_puzzle_hash = None
                        # Find coin that doesn't belong to us
                        amount = 0
                        for coin in additions:
                            derivation_record = await self.puzzle_store.get_derivation_record_for_puzzle_hash(
                                coin.puzzle_hash
                            )
                            if derivation_record is None:
                                to_puzzle_hash = coin.puzzle_hash
                                amount += coin.amount

                        if to_puzzle_hash is None:
                            to_puzzle_hash = additions[0].puzzle_hash

                        spent_timestamp = await self.wallet_node.get_timestamp_for_height(coin_state.spent_height)

                        # Reorg rollback adds reorged transactions so it's possible there is tx_record already
                        # Even though we are just adding coin record to the db (after reorg)
                        tx_records: List[TransactionRecord] = []
                        for out_tx_record in all_outgoing:
                            for rem_coin in out_tx_record.removals:
                                if rem_coin.name() == coin_state.coin.name():
                                    tx_records.append(out_tx_record)

                        if len(tx_records) > 0:
                            for tx_record in tx_records:
                                await self.tx_store.set_confirmed(tx_record.name, coin_state.spent_height)
                        else:
                            tx_record = TransactionRecord(
                                confirmed_at_height=coin_state.spent_height,
                                created_at_time=uint64(spent_timestamp),
                                to_puzzle_hash=(await self.convert_puzzle_hash(wallet_id, to_puzzle_hash)),
                                amount=uint64(int(amount)),
                                fee_amount=uint64(fee),
                                confirmed=True,
                                sent=uint32(0),
                                spend_bundle=None,
                                additions=additions,
                                removals=[coin_state.coin],
                                wallet_id=wallet_id,
                                sent_to=[],
                                trade_id=None,
                                type=uint32(TransactionType.OUTGOING_TX.value),
                                name=bytes32(token_bytes()),
                                memos=[],
                            )

                            await self.tx_store.add_transaction_record(tx_record, True)
                else:
                    await self.coin_store.set_spent(coin_state.coin.name(), coin_state.spent_height)
                    rem_tx_records: List[TransactionRecord] = []
                    for out_tx_record in all_outgoing:
                        for rem_coin in out_tx_record.removals:
                            if rem_coin.name() == coin_state.coin.name():
                                rem_tx_records.append(out_tx_record)

                    for tx_record in rem_tx_records:
                        await self.tx_store.set_confirmed(tx_record.name, coin_state.spent_height)
                for unconfirmed_record in all_unconfirmed:
                    for rem_coin in unconfirmed_record.removals:
                        if rem_coin.name() == coin_state.coin.name():
                            self.log.info(f"Setting tx_id: {unconfirmed_record.name} to confirmed")
                            await self.tx_store.set_confirmed(unconfirmed_record.name, coin_state.spent_height)

                if record.wallet_type == WalletType.POOLING_WALLET:
                    if coin_state.spent_height is not None and coin_state.coin.amount == uint64(1):
                        wallet = self.wallets[uint32(record.wallet_id)]
                        curr_coin_state: CoinState = coin_state

                        while curr_coin_state.spent_height is not None:
                            cs: CoinSpend = await self.wallet_node.fetch_puzzle_solution(
                                peer, curr_coin_state.spent_height, curr_coin_state.coin
                            )
                            success = await wallet.apply_state_transition(cs, curr_coin_state.spent_height)
                            if not success:
                                break
                            new_singleton_coin: Optional[Coin] = wallet.get_next_interesting_coin(cs)
                            if new_singleton_coin is None:
                                # No more singleton (maybe destroyed?)
                                break
                            await self.coin_added(
                                new_singleton_coin,
                                coin_state.spent_height,
                                [],
                                uint32(record.wallet_id),
                                record.wallet_type,
                            )
                            await self.coin_store.set_spent(curr_coin_state.coin.name(), curr_coin_state.spent_height)
                            await self.add_interested_coin_ids([new_singleton_coin.name()], True)
                            new_coin_state: List[CoinState] = await self.wallet_node.get_coin_state(
                                [new_singleton_coin.name()], fork_height, peer
                            )
                            assert len(new_coin_state) == 1
                            curr_coin_state = new_coin_state[0]

                # Check if a child is a singleton launcher
                if children is None:
                    children = await self.wallet_node.fetch_children(peer, coin_state.coin.name(), fork_height)
                assert children is not None
                for child in children:
                    if child.coin.puzzle_hash != SINGLETON_LAUNCHER_HASH:
                        continue
                    if await self.have_a_pool_wallet_with_launched_id(child.coin.name()):
                        continue
                    if child.spent_height is None:
                        # TODO handle spending launcher later block
                        continue
                    launcher_spend: Optional[CoinSpend] = await self.wallet_node.fetch_puzzle_solution(
                        peer, coin_state.spent_height, child.coin
                    )
                    if launcher_spend is None:
                        continue
                    try:
                        pool_state = solution_to_pool_state(launcher_spend)
                    except Exception as e:
                        self.log.debug(f"Not a pool wallet launcher {e}")
                        continue

                    # solution_to_pool_state may return None but this may not be an error
                    if pool_state is None:
                        self.log.debug("solution_to_pool_state returned None, ignore and continue")
                        continue

                    assert child.spent_height is not None
                    pool_wallet = await PoolWallet.create(
                        self,
                        self.main_wallet,
                        child.coin.name(),
                        [launcher_spend],
                        child.spent_height,
                        True,
                        "pool_wallet",
                    )
                    launcher_spend_additions = launcher_spend.additions()
                    assert len(launcher_spend_additions) == 1
                    coin_added = launcher_spend_additions[0]
                    await self.coin_added(
                        coin_added, coin_state.spent_height, [], pool_wallet.id(), WalletType(pool_wallet.type())
                    )
                    await self.add_interested_coin_ids([coin_added.name()], True)

            else:
                raise RuntimeError("All cases already handled")  # Logic error, all cases handled
        for coin_state_removed in trade_coin_removed:
            await self.trade_manager.coins_of_interest_farmed(coin_state_removed, fork_height)

    async def have_a_pool_wallet_with_launched_id(self, launcher_id: bytes32) -> bool:
        for wallet_id, wallet in self.wallets.items():
            if (
                wallet.type() == WalletType.POOLING_WALLET
                and (await wallet.get_current_state()).launcher_id == launcher_id
            ):
                self.log.warning("Already have, not recreating")
                return True
        return False

    def is_pool_reward(self, created_height, parent_id):
        for i in range(0, 30):
            try_height = created_height - i
            if try_height < 0:
                break
            calculated = pool_parent_id(try_height, self.constants.GENESIS_CHALLENGE)
            if calculated == parent_id:
                return True
        return False

    def is_farmer_reward(self, created_height, parent_id):
        for i in range(0, 30):
            try_height = created_height - i
            if try_height < 0:
                break
            calculated = farmer_parent_id(try_height, self.constants.GENESIS_CHALLENGE)
            if calculated == parent_id:
                return True
        return False

    async def get_wallet_id_for_puzzle_hash(self, puzzle_hash: bytes32) -> Optional[Tuple[uint32, WalletType]]:
        info = await self.puzzle_store.wallet_info_for_puzzle_hash(puzzle_hash)
        if info is not None:
            wallet_id, wallet_type = info
            return uint32(wallet_id), wallet_type

        interested_wallet_id = await self.interested_store.get_interested_puzzle_hash_wallet_id(puzzle_hash=puzzle_hash)
        if interested_wallet_id is not None:
            wallet_id = uint32(interested_wallet_id)
            if wallet_id not in self.wallets.keys():
                self.log.warning(f"Do not have wallet {wallet_id} for puzzle_hash {puzzle_hash}")
                return None
            wallet_type = WalletType(self.wallets[uint32(wallet_id)].type())
            return uint32(wallet_id), wallet_type
        return None

    async def coin_added(
        self,
        coin: Coin,
        height: uint32,
        all_outgoing_transaction_records: List[TransactionRecord],
        wallet_id: uint32,
        wallet_type: WalletType,
    ) -> Optional[WalletCoinRecord]:
        """
        Adding coin to DB, return wallet coin record if it get's added
        """
        existing: Optional[WalletCoinRecord] = await self.coin_store.get_coin_record(coin.name())
        if existing is not None:
            return None

        self.log.info(f"Adding coin: {coin} at {height} wallet_id:{wallet_id}")
        farmer_reward = False
        pool_reward = False
        if self.is_farmer_reward(height, coin.parent_coin_info):
            farmer_reward = True
        elif self.is_pool_reward(height, coin.parent_coin_info):
            pool_reward = True

        farm_reward = False
        coin_record: Optional[WalletCoinRecord] = await self.coin_store.get_coin_record(coin.parent_coin_info)
        if coin_record is not None and wallet_type.value == coin_record.wallet_type:
            change = True
        else:
            change = False

        if farmer_reward or pool_reward:
            farm_reward = True
            if pool_reward:
                tx_type: int = TransactionType.COINBASE_REWARD.value
            else:
                tx_type = TransactionType.FEE_REWARD.value
            timestamp = await self.wallet_node.get_timestamp_for_height(height)

            tx_record = TransactionRecord(
                confirmed_at_height=uint32(height),
                created_at_time=timestamp,
                to_puzzle_hash=(await self.convert_puzzle_hash(wallet_id, coin.puzzle_hash)),
                amount=coin.amount,
                fee_amount=uint64(0),
                confirmed=True,
                sent=uint32(0),
                spend_bundle=None,
                additions=[coin],
                removals=[],
                wallet_id=wallet_id,
                sent_to=[],
                trade_id=None,
                type=uint32(tx_type),
                name=coin.name(),
                memos=[],
            )
            await self.tx_store.add_transaction_record(tx_record, True)
        else:
            records: List[TransactionRecord] = []
            for record in all_outgoing_transaction_records:
                for add_coin in record.additions:
                    if add_coin.name() == coin.name():
                        records.append(record)

            if len(records) > 0:
                for record in records:
                    if record.confirmed is False:
                        await self.tx_store.set_confirmed(record.name, height)
            elif not change:
                timestamp = await self.wallet_node.get_timestamp_for_height(height)
                tx_record = TransactionRecord(
                    confirmed_at_height=uint32(height),
                    created_at_time=timestamp,
                    to_puzzle_hash=(await self.convert_puzzle_hash(wallet_id, coin.puzzle_hash)),
                    amount=coin.amount,
                    fee_amount=uint64(0),
                    confirmed=True,
                    sent=uint32(0),
                    spend_bundle=None,
                    additions=[coin],
                    removals=[],
                    wallet_id=wallet_id,
                    sent_to=[],
                    trade_id=None,
                    type=uint32(TransactionType.INCOMING_TX.value),
                    name=coin.name(),
                    memos=[],
                )
                if coin.amount > 0:
                    await self.tx_store.add_transaction_record(tx_record, True)

        coin_record_1: WalletCoinRecord = WalletCoinRecord(
            coin, height, uint32(0), False, farm_reward, wallet_type, wallet_id
        )
        await self.coin_store.add_coin_record(coin_record_1)

        if wallet_type == WalletType.CAT or wallet_type == WalletType.DISTRIBUTED_ID:
            wallet = self.wallets[wallet_id]
            await wallet.coin_added(coin, height)

        await self.create_more_puzzle_hashes(in_transaction=True)
        return coin_record_1

    async def add_pending_transaction(self, tx_record: TransactionRecord):
        """
        Called from wallet before new transaction is sent to the full_node
        """
        # Wallet node will use this queue to retry sending this transaction until full nodes receives it
        await self.tx_store.add_transaction_record(tx_record, False)
        all_coins_names = []
        all_coins_names.extend([coin.name() for coin in tx_record.additions])
        all_coins_names.extend([coin.name() for coin in tx_record.removals])

        await self.add_interested_coin_ids(all_coins_names, False)
        self.tx_pending_changed()
        self.state_changed("pending_transaction", tx_record.wallet_id)

    async def add_transaction(self, tx_record: TransactionRecord, in_transaction=False):
        """
        Called from wallet to add transaction that is not being set to full_node
        """
        await self.tx_store.add_transaction_record(tx_record, in_transaction)
        self.state_changed("pending_transaction", tx_record.wallet_id)

    async def remove_from_queue(
        self,
        spendbundle_id: bytes32,
        name: str,
        send_status: MempoolInclusionStatus,
        error: Optional[Err],
    ):
        """
        Full node received our transaction, no need to keep it in queue anymore
        """
        updated = await self.tx_store.increment_sent(spendbundle_id, name, send_status, error)
        if updated:
            tx: Optional[TransactionRecord] = await self.get_transaction(spendbundle_id)
            if tx is not None:
                self.state_changed("tx_update", tx.wallet_id, {"transaction": tx})

    async def get_all_transactions(self, wallet_id: int) -> List[TransactionRecord]:
        """
        Retrieves all confirmed and pending transactions
        """
        records = await self.tx_store.get_all_transactions_for_wallet(wallet_id)
        return records

    async def get_transaction(self, tx_id: bytes32) -> Optional[TransactionRecord]:
        return await self.tx_store.get_transaction_record(tx_id)

    async def is_addition_relevant(self, addition: Coin):
        """
        Check whether we care about a new addition (puzzle_hash). Returns true if we
        control this puzzle hash.
        """
        result = await self.puzzle_store.puzzle_hash_exists(addition.puzzle_hash)
        return result

    async def get_wallet_for_coin(self, coin_id: bytes32) -> Any:
        coin_record = await self.coin_store.get_coin_record(coin_id)
        if coin_record is None:
            return None
        wallet_id = uint32(coin_record.wallet_id)
        wallet = self.wallets[wallet_id]
        return wallet

    async def reorg_rollback(self, height: int):
        """
        Rolls back and updates the coin_store and transaction store. It's possible this height
        is the tip, or even beyond the tip.
        """
        try:
            await self.db_wrapper.commit_transaction()
            await self.db_wrapper.begin_transaction()

            await self.coin_store.rollback_to_block(height)
            reorged: List[TransactionRecord] = await self.tx_store.get_transaction_above(height)
            await self.tx_store.rollback_to_block(height)
            for record in reorged:
                if record.type in [
                    TransactionType.OUTGOING_TX,
                    TransactionType.OUTGOING_TRADE,
                    TransactionType.INCOMING_TRADE,
                ]:
                    await self.tx_store.tx_reorged(record, in_transaction=True)
            self.tx_pending_changed()

            # Removes wallets that were created from a blockchain transaction which got reorged.
            remove_ids = []
            for wallet_id, wallet in self.wallets.items():
                if wallet.type() == WalletType.POOLING_WALLET.value:
                    remove: bool = await wallet.rewind(height, in_transaction=True)
                    if remove:
                        remove_ids.append(wallet_id)
            for wallet_id in remove_ids:
                await self.user_store.delete_wallet(wallet_id, in_transaction=True)
                self.wallets.pop(wallet_id)
            await self.db_wrapper.commit_transaction()
        except Exception as e:
            tb = traceback.format_exc()
            self.log.error(f"Exception while rolling back: {e} {tb}")
            await self.db_wrapper.rollback_transaction()
            await self.coin_store.rebuild_wallet_cache()
            await self.tx_store.rebuild_tx_cache()
            await self.pool_store.rebuild_cache()
            raise

    async def _await_closed(self) -> None:
        await self.db_connection.close()
        if self.weight_proof_handler is not None:
            self.weight_proof_handler.cancel_weight_proof_tasks()

    def unlink_db(self):
        Path(self.db_path).unlink()

    async def get_all_wallet_info_entries(self) -> List[WalletInfo]:
        return await self.user_store.get_all_wallet_info_entries()

    async def get_start_height(self):
        """
        If we have coin use that as starting height next time,
        otherwise use the peak
        """

        return 0

    async def get_wallet_for_asset_id(self, asset_id: str):
        for wallet_id in self.wallets:
            wallet = self.wallets[wallet_id]
            if wallet.type() == WalletType.CAT:
                if bytes(wallet.cat_info.limitations_program_hash).hex() == asset_id:
                    return wallet
        return None

    async def add_new_wallet(self, wallet: Any, wallet_id: int, create_puzzle_hashes=True, in_transaction=False):
        self.wallets[uint32(wallet_id)] = wallet
        if create_puzzle_hashes:
            await self.create_more_puzzle_hashes(in_transaction=in_transaction)
        self.state_changed("wallet_created")

    async def get_spendable_coins_for_wallet(self, wallet_id: int, records=None) -> Set[WalletCoinRecord]:
        if records is None:
            records = await self.coin_store.get_unspent_coins_for_wallet(wallet_id)

        # Coins that are currently part of a transaction
        unconfirmed_tx: List[TransactionRecord] = await self.tx_store.get_unconfirmed_for_wallet(wallet_id)
        removal_dict: Dict[bytes32, Coin] = {}
        for tx in unconfirmed_tx:
            for coin in tx.removals:
                # TODO, "if" might not be necessary once unconfirmed tx doesn't contain coins for other wallets
                if await self.does_coin_belong_to_wallet(coin, wallet_id):
                    removal_dict[coin.name()] = coin

        # Coins that are part of the trade
        offer_locked_coins: Dict[bytes32, WalletCoinRecord] = await self.trade_manager.get_locked_coins()

        filtered = set()
        for record in records:
            if record.coin.name() in offer_locked_coins:
                continue
            if record.coin.name() in removal_dict:
                continue
            filtered.add(record)

        return filtered

    async def create_action(
        self, name: str, wallet_id: int, wallet_type: int, callback: str, done: bool, data: str, in_transaction: bool
    ):
        await self.action_store.create_action(name, wallet_id, wallet_type, callback, done, data, in_transaction)
        self.tx_pending_changed()

    async def generator_received(self, height: uint32, header_hash: uint32, program: Program):

        actions: List[WalletAction] = await self.action_store.get_all_pending_actions()
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_generator":
                stored_header_hash = bytes32(hexstr_to_bytes(action_data["header_hash"]))
                stored_height = uint32(action_data["height"])
                if stored_header_hash == header_hash and stored_height == height:
                    if action.done:
                        return None
                    wallet = self.wallets[uint32(action.wallet_id)]
                    callback_str = action.wallet_callback
                    if callback_str is not None:
                        callback = getattr(wallet, callback_str)
                        await callback(height, header_hash, program, action.id)

    async def puzzle_solution_received(self, response: RespondPuzzleSolution):
        unwrapped: PuzzleSolutionResponse = response.response
        actions: List[WalletAction] = await self.action_store.get_all_pending_actions()
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_puzzle_solution":
                stored_coin_name = bytes32(hexstr_to_bytes(action_data["coin_name"]))
                height = uint32(action_data["height"])
                if stored_coin_name == unwrapped.coin_name and height == unwrapped.height:
                    if action.done:
                        return None
                    wallet = self.wallets[uint32(action.wallet_id)]
                    callback_str = action.wallet_callback
                    if callback_str is not None:
                        callback = getattr(wallet, callback_str)
                        await callback(unwrapped, action.id)

    async def new_peak(self, peak: wallet_protocol.NewPeakWallet):
        for wallet_id, wallet in self.wallets.items():
            if wallet.type() == uint8(WalletType.POOLING_WALLET):
                await wallet.new_peak(peak.height)

    async def add_interested_puzzle_hashes(
        self, puzzle_hashes: List[bytes32], wallet_ids: List[int], in_transaction: bool = False
    ) -> None:
        for puzzle_hash, wallet_id in zip(puzzle_hashes, wallet_ids):
            await self.interested_store.add_interested_puzzle_hash(puzzle_hash, wallet_id, in_transaction)
        if len(puzzle_hashes) > 0:
            await self.wallet_node.new_peak_queue.subscribe_to_puzzle_hashes(puzzle_hashes)

    async def add_interested_coin_ids(self, coin_ids: List[bytes32], in_transaction: bool = False) -> None:
        for coin_id in coin_ids:
            await self.interested_store.add_interested_coin_id(coin_id, in_transaction)
        if len(coin_ids) > 0:
            await self.wallet_node.new_peak_queue.subscribe_to_coin_ids(coin_ids)

    async def delete_trade_transactions(self, trade_id: bytes32):
        txs: List[TransactionRecord] = await self.tx_store.get_transactions_by_trade_id(trade_id)
        for tx in txs:
            await self.tx_store.delete_transaction_record(tx.name)

    async def convert_puzzle_hash(self, wallet_id: uint32, puzzle_hash: bytes32) -> bytes32:
        wallet = self.wallets[wallet_id]
        # This should be general to wallets but for right now this is just for CATs so we'll add this if
        if wallet.type() == WalletType.CAT.value:
            return await wallet.convert_puzzle_hash(puzzle_hash)

        return puzzle_hash
