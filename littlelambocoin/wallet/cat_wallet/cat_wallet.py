from __future__ import annotations

import dataclasses
import logging
import time
from secrets import token_bytes
from typing import Any, Dict, List, Optional, Set, Tuple

from blspy import AugSchemeMPL, G2Element

from littlelambocoin.consensus.cost_calculator import NPCResult
from littlelambocoin.full_node.bundle_tools import simple_solution_generator
from littlelambocoin.full_node.mempool_check_conditions import get_name_puzzle_conditions
from littlelambocoin.protocols.wallet_protocol import CoinState
from littlelambocoin.types.blockchain_format.coin import Coin
from littlelambocoin.types.blockchain_format.program import Program
from littlelambocoin.types.blockchain_format.sized_bytes import bytes32
from littlelambocoin.types.announcement import Announcement
from littlelambocoin.types.coin_spend import CoinSpend
from littlelambocoin.types.generator_types import BlockGenerator
from littlelambocoin.types.spend_bundle import SpendBundle
from littlelambocoin.types.condition_opcodes import ConditionOpcode
from littlelambocoin.util.byte_types import hexstr_to_bytes
from littlelambocoin.util.condition_tools import conditions_dict_for_solution, pkm_pairs_for_conditions_dict
from littlelambocoin.util.hash import std_hash
from littlelambocoin.util.ints import uint8, uint32, uint64, uint128
from littlelambocoin.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from littlelambocoin.wallet.cat_wallet.cat_info import CATInfo, LegacyCATInfo
from littlelambocoin.wallet.cat_wallet.cat_utils import (
    CAT_MOD,
    SpendableCAT,
    construct_cat_puzzle,
    unsigned_spend_bundle_for_spendable_cats,
    match_cat_puzzle,
)
from littlelambocoin.wallet.derivation_record import DerivationRecord
from littlelambocoin.wallet.cat_wallet.lineage_store import CATLineageStore
from littlelambocoin.wallet.lineage_proof import LineageProof
from littlelambocoin.wallet.payment import Payment
from littlelambocoin.wallet.puzzles.genesis_checkers import ALL_LIMITATIONS_PROGRAMS
from littlelambocoin.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import (
    DEFAULT_HIDDEN_PUZZLE_HASH,
    calculate_synthetic_secret_key,
)
from littlelambocoin.wallet.transaction_record import TransactionRecord
from littlelambocoin.wallet.util.transaction_type import TransactionType
from littlelambocoin.wallet.util.wallet_types import WalletType, AmountWithPuzzlehash
from littlelambocoin.wallet.wallet import Wallet
from littlelambocoin.wallet.wallet_coin_record import WalletCoinRecord
from littlelambocoin.wallet.wallet_info import WalletInfo
from littlelambocoin.wallet.util.compute_memos import compute_memos
import traceback


# This should probably not live in this file but it's for experimental right now


class CATWallet:
    wallet_state_manager: Any
    log: logging.Logger
    wallet_info: WalletInfo
    cat_info: CATInfo
    standard_wallet: Wallet
    cost_of_single_tx: Optional[int]
    lineage_store: CATLineageStore

    @staticmethod
    def default_wallet_name_for_unknown_cat(limitations_program_hash_hex: str) -> str:
        return f"CAT {limitations_program_hash_hex[:16]}..."

    @staticmethod
    async def create_new_cat_wallet(
        wallet_state_manager: Any, wallet: Wallet, cat_tail_info: Dict[str, Any], amount: uint64, name=None
    ):
        self = CATWallet()
        self.cost_of_single_tx = None
        self.standard_wallet = wallet
        self.log = logging.getLogger(__name__)
        std_wallet_id = self.standard_wallet.wallet_id
        bal = await wallet_state_manager.get_confirmed_balance_for_wallet(std_wallet_id)
        if amount > bal:
            raise ValueError("Not enough balance")
        self.wallet_state_manager = wallet_state_manager

        # We use 00 bytes because it's not optional. We must check this is overidden during issuance.
        empty_bytes = bytes32(32 * b"\0")
        self.cat_info = CATInfo(empty_bytes, None)
        info_as_string = bytes(self.cat_info).hex()
        # If the name is not provided, it will be autogenerated based on the resulting tail hash.
        # For now, give the wallet a temporary name "CAT WALLET" until we get the tail hash
        original_name = name
        if name is None:
            name = "CAT WALLET"

        self.wallet_info = await wallet_state_manager.user_store.create_wallet(name, WalletType.CAT, info_as_string)
        if self.wallet_info is None:
            raise ValueError("Internal Error")

        self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())

        try:
            littlelambocoin_tx, spend_bundle = await ALL_LIMITATIONS_PROGRAMS[
                cat_tail_info["identifier"]
            ].generate_issuance_bundle(
                self,
                cat_tail_info,
                amount,
            )
            assert self.cat_info.limitations_program_hash != empty_bytes
        except Exception:
            await wallet_state_manager.user_store.delete_wallet(self.id(), False)
            raise
        if spend_bundle is None:
            await wallet_state_manager.user_store.delete_wallet(self.id())
            raise ValueError("Failed to create spend.")

        await self.wallet_state_manager.add_new_wallet(self, self.id())

        # If the new CAT name wasn't originally provided, we used a temporary name before issuance
        # since we didn't yet know the TAIL. Now we know the TAIL, we can update the name
        # according to the template name for unknown/new CATs.
        if original_name is None:
            name = self.default_wallet_name_for_unknown_cat(self.cat_info.limitations_program_hash.hex())
            await self.set_name(name)

        # Change and actual CAT coin
        non_ephemeral_coins: List[Coin] = spend_bundle.not_ephemeral_additions()
        cat_coin = None
        puzzle_store = self.wallet_state_manager.puzzle_store
        for c in non_ephemeral_coins:
            info = await puzzle_store.wallet_info_for_puzzle_hash(c.puzzle_hash)
            if info is None:
                raise ValueError("Internal Error")
            id, wallet_type = info
            if id == self.id():
                cat_coin = c

        if cat_coin is None:
            raise ValueError("Internal Error, unable to generate new CAT coin")
        cat_pid: bytes32 = cat_coin.parent_coin_info

        cat_record = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=uint64(int(time.time())),
            to_puzzle_hash=(await self.convert_puzzle_hash(cat_coin.puzzle_hash)),
            amount=uint64(cat_coin.amount),
            fee_amount=uint64(0),
            confirmed=False,
            sent=uint32(10),
            spend_bundle=None,
            additions=[cat_coin],
            removals=list(filter(lambda rem: rem.name() == cat_pid, spend_bundle.removals())),
            wallet_id=self.id(),
            sent_to=[],
            trade_id=None,
            type=uint32(TransactionType.INCOMING_TX.value),
            name=bytes32(token_bytes()),
            memos=[],
        )
        littlelambocoin_tx = dataclasses.replace(littlelambocoin_tx, spend_bundle=spend_bundle)
        await self.standard_wallet.push_transaction(littlelambocoin_tx)
        await self.standard_wallet.push_transaction(cat_record)
        return self

    @staticmethod
    async def create_wallet_for_cat(
        wallet_state_manager: Any,
        wallet: Wallet,
        limitations_program_hash_hex: str,
        name=None,
        in_transaction=False,
    ) -> CATWallet:
        self = CATWallet()
        self.cost_of_single_tx = None
        self.standard_wallet = wallet
        self.log = logging.getLogger(__name__)

        limitations_program_hash_hex = bytes32.from_hexstr(limitations_program_hash_hex).hex()  # Normalize the format

        for id, wallet in wallet_state_manager.wallets.items():
            if wallet.type() == CATWallet.type():
                if wallet.get_asset_id() == limitations_program_hash_hex:  # type: ignore
                    self.log.warning("Not creating wallet for already existing CAT wallet")
                    raise ValueError("Wallet already exists")

        self.wallet_state_manager = wallet_state_manager
        if limitations_program_hash_hex in DEFAULT_CATS:
            cat_info = DEFAULT_CATS[limitations_program_hash_hex]
            name = cat_info["name"]
        elif name is None:
            name = self.default_wallet_name_for_unknown_cat(limitations_program_hash_hex)

        limitations_program_hash = bytes32(hexstr_to_bytes(limitations_program_hash_hex))
        self.cat_info = CATInfo(limitations_program_hash, None)
        info_as_string = bytes(self.cat_info).hex()
        self.wallet_info = await wallet_state_manager.user_store.create_wallet(
            name, WalletType.CAT, info_as_string, in_transaction=in_transaction
        )
        if self.wallet_info is None:
            raise Exception("wallet_info is None")

        self.lineage_store = await CATLineageStore.create(
            self.wallet_state_manager.db_wrapper, self.get_asset_id(), in_transaction=in_transaction
        )
        await self.wallet_state_manager.add_new_wallet(self, self.id(), in_transaction=in_transaction)
        return self

    @staticmethod
    async def create(
        wallet_state_manager: Any,
        wallet: Wallet,
        wallet_info: WalletInfo,
    ) -> CATWallet:
        self = CATWallet()

        self.log = logging.getLogger(__name__)

        self.cost_of_single_tx = None
        self.wallet_state_manager = wallet_state_manager
        self.wallet_info = wallet_info
        self.standard_wallet = wallet
        try:
            self.cat_info = CATInfo.from_bytes(hexstr_to_bytes(self.wallet_info.data))
            self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())
        except AssertionError:
            # Do a migration of the lineage proofs
            cat_info = LegacyCATInfo.from_bytes(hexstr_to_bytes(self.wallet_info.data))
            self.cat_info = CATInfo(cat_info.limitations_program_hash, cat_info.my_tail)
            self.lineage_store = await CATLineageStore.create(self.wallet_state_manager.db_wrapper, self.get_asset_id())
            for coin_id, lineage in cat_info.lineage_proofs:
                await self.add_lineage(coin_id, lineage, False)
            await self.save_info(self.cat_info, False)

        return self

    @classmethod
    def type(cls) -> uint8:
        return uint8(WalletType.CAT)

    def id(self) -> uint32:
        return self.wallet_info.id

    async def get_confirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint64:
        if record_list is None:
            record_list = await self.wallet_state_manager.coin_store.get_unspent_coins_for_wallet(self.id())

        amount: uint64 = uint64(0)
        for record in record_list:
            lineage = await self.get_lineage_proof_for_coin(record.coin)
            if lineage is not None:
                amount = uint64(amount + record.coin.amount)

        self.log.info(f"Confirmed balance for cat wallet {self.id()} is {amount}")
        return uint64(amount)

    async def get_unconfirmed_balance(self, unspent_records=None) -> uint128:
        return await self.wallet_state_manager.get_unconfirmed_balance(self.id(), unspent_records)

    async def get_max_send_amount(self, records=None):
        spendable: List[WalletCoinRecord] = list(await self.get_cat_spendable_coins())
        if len(spendable) == 0:
            return 0
        spendable.sort(reverse=True, key=lambda record: record.coin.amount)
        if self.cost_of_single_tx is None:
            coin = spendable[0].coin
            txs = await self.generate_signed_transaction(
                [coin.amount], [coin.puzzle_hash], coins={coin}, ignore_max_send_amount=True
            )
            program: BlockGenerator = simple_solution_generator(txs[0].spend_bundle)
            # npc contains names of the coins removed, puzzle_hashes and their spend conditions
            result: NPCResult = get_name_puzzle_conditions(
                program,
                self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
                cost_per_byte=self.wallet_state_manager.constants.COST_PER_BYTE,
                mempool_mode=True,
            )
            self.cost_of_single_tx = result.cost
            self.log.info(f"Cost of a single tx for CAT wallet: {self.cost_of_single_tx}")

        max_cost = self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM / 2  # avoid full block TXs
        current_cost = 0
        total_amount = 0
        total_coin_count = 0

        for record in spendable:
            current_cost += self.cost_of_single_tx
            total_amount += record.coin.amount
            total_coin_count += 1
            if current_cost + self.cost_of_single_tx > max_cost:
                break

        return total_amount

    async def get_name(self):
        return self.wallet_info.name

    async def set_name(self, new_name: str):
        new_info = dataclasses.replace(self.wallet_info, name=new_name)
        self.wallet_info = new_info
        await self.wallet_state_manager.user_store.update_wallet(self.wallet_info, False)

    def get_asset_id(self) -> str:
        return bytes(self.cat_info.limitations_program_hash).hex()

    async def set_tail_program(self, tail_program: str):
        assert Program.fromhex(tail_program).get_tree_hash() == self.cat_info.limitations_program_hash
        await self.save_info(
            CATInfo(
                self.cat_info.limitations_program_hash,
                Program.fromhex(tail_program),
            ),
            False,
        )

    async def coin_added(self, coin: Coin, height: uint32):
        """Notification from wallet state manager that wallet has been received."""
        self.log.info(f"CAT wallet has been notified that {coin} was added")

        inner_puzzle = await self.inner_puzzle_for_cat_puzhash(coin.puzzle_hash)
        lineage_proof = LineageProof(coin.parent_coin_info, inner_puzzle.get_tree_hash(), coin.amount)
        await self.add_lineage(coin.name(), lineage_proof, True)

        lineage = await self.get_lineage_proof_for_coin(coin)

        if lineage is None:
            for node_id, node in self.wallet_state_manager.wallet_node.server.all_connections.items():
                try:
                    coin_state = await self.wallet_state_manager.wallet_node.get_coin_state(
                        [coin.parent_coin_info], None, node
                    )
                    assert coin_state[0].coin.name() == coin.parent_coin_info
                    coin_spend = await self.wallet_state_manager.wallet_node.fetch_puzzle_solution(
                        node, coin_state[0].spent_height, coin_state[0].coin
                    )
                    await self.puzzle_solution_received(coin_spend)
                    break
                except Exception as e:
                    self.log.debug(f"Exception: {e}, traceback: {traceback.format_exc()}")

    async def puzzle_solution_received(self, coin_spend: CoinSpend):
        coin_name = coin_spend.coin.name()
        puzzle: Program = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
        matched, curried_args = match_cat_puzzle(puzzle)
        if matched:
            mod_hash, genesis_coin_checker_hash, inner_puzzle = curried_args
            self.log.info(f"parent: {coin_name} inner_puzzle for parent is {inner_puzzle}")
            parent_coin = None
            coin_record = await self.wallet_state_manager.coin_store.get_coin_record(coin_name)
            if coin_record is None:
                coin_states: Optional[List[CoinState]] = await self.wallet_state_manager.wallet_node.get_coin_state(
                    [coin_name]
                )
                if coin_states is not None:
                    parent_coin = coin_states[0].coin
            if coin_record is not None:
                parent_coin = coin_record.coin
            if parent_coin is None:
                raise ValueError("Error in finding parent")
            await self.add_lineage(
                coin_name,
                LineageProof(parent_coin.parent_coin_info, inner_puzzle.get_tree_hash(), parent_coin.amount),
                True,
            )
        else:
            # The parent is not a CAT which means we need to scrub all of its children from our DB
            child_coin_records = await self.wallet_state_manager.coin_store.get_coin_records_by_parent_id(coin_name)
            if len(child_coin_records) > 0:
                for record in child_coin_records:
                    if record.wallet_id == self.id():
                        await self.wallet_state_manager.coin_store.delete_coin_record(record.coin.name())
                        await self.remove_lineage(record.coin.name())
                        # We also need to make sure there's no record of the transaction
                        await self.wallet_state_manager.tx_store.delete_transaction_record(record.coin.name())

    async def get_new_inner_hash(self) -> bytes32:
        puzzle = await self.get_new_inner_puzzle()
        return puzzle.get_tree_hash()

    async def get_new_inner_puzzle(self) -> Program:
        return await self.standard_wallet.get_new_puzzle()

    async def get_new_puzzlehash(self) -> bytes32:
        return await self.standard_wallet.get_new_puzzlehash()

    def puzzle_for_pk(self, pubkey) -> Program:
        inner_puzzle = self.standard_wallet.puzzle_for_pk(bytes(pubkey))
        cat_puzzle: Program = construct_cat_puzzle(CAT_MOD, self.cat_info.limitations_program_hash, inner_puzzle)
        return cat_puzzle

    async def get_new_cat_puzzle_hash(self):
        return (await self.wallet_state_manager.get_unused_derivation_record(self.id())).puzzle_hash

    async def get_spendable_balance(self, records=None) -> uint64:
        coins = await self.get_cat_spendable_coins(records)
        amount = 0
        for record in coins:
            amount += record.coin.amount

        return uint64(amount)

    async def get_pending_change_balance(self) -> uint64:
        unconfirmed_tx = await self.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(self.id())
        addition_amount = 0
        for record in unconfirmed_tx:
            if not record.is_in_mempool():
                continue
            our_spend = False
            for coin in record.removals:
                if await self.wallet_state_manager.does_coin_belong_to_wallet(coin, self.id()):
                    our_spend = True
                    break

            if our_spend is not True:
                continue

            for coin in record.additions:
                if await self.wallet_state_manager.does_coin_belong_to_wallet(coin, self.id()):
                    addition_amount += coin.amount

        return uint64(addition_amount)

    async def get_cat_spendable_coins(self, records=None) -> List[WalletCoinRecord]:
        result: List[WalletCoinRecord] = []

        record_list: Set[WalletCoinRecord] = await self.wallet_state_manager.get_spendable_coins_for_wallet(
            self.id(), records
        )

        for record in record_list:
            lineage = await self.get_lineage_proof_for_coin(record.coin)
            if lineage is not None and not lineage.is_none():
                result.append(record)

        return result

    async def select_coins(self, amount: uint64) -> Set[Coin]:
        """
        Returns a set of coins that can be used for generating a new transaction.
        Note: Must be called under wallet state manager lock
        """

        spendable_am = await self.get_confirmed_balance()

        if amount > spendable_am:
            error_msg = f"Can't select amount higher than our spendable balance {amount}, spendable {spendable_am}"
            self.log.warning(error_msg)
            raise ValueError(error_msg)

        self.log.info(f"About to select coins for amount {amount}")
        spendable: List[WalletCoinRecord] = await self.get_cat_spendable_coins()

        sum = 0
        used_coins: Set = set()

        # Use older coins first
        spendable.sort(key=lambda r: r.confirmed_block_height)

        # Try to use coins from the store, if there isn't enough of "unused"
        # coins use change coins that are not confirmed yet
        unconfirmed_removals: Dict[bytes32, Coin] = await self.wallet_state_manager.unconfirmed_removals_for_wallet(
            self.id()
        )
        for coinrecord in spendable:
            if sum >= amount and len(used_coins) > 0:
                break
            if coinrecord.coin.name() in unconfirmed_removals:
                continue
            sum += coinrecord.coin.amount
            used_coins.add(coinrecord.coin)
            self.log.info(f"Selected coin: {coinrecord.coin.name()} at height {coinrecord.confirmed_block_height}!")

        # This happens when we couldn't use one of the coins because it's already used
        # but unconfirmed, and we are waiting for the change. (unconfirmed_additions)
        if sum < amount:
            raise ValueError(
                "Can't make this transaction at the moment. Waiting for the change from the previous transaction."
            )

        self.log.info(f"Successfully selected coins: {used_coins}")
        return used_coins

    async def sign(self, spend_bundle: SpendBundle) -> SpendBundle:
        sigs: List[G2Element] = []
        for spend in spend_bundle.coin_spends:
            matched, puzzle_args = match_cat_puzzle(spend.puzzle_reveal.to_program())
            if matched:
                _, _, inner_puzzle = puzzle_args
                puzzle_hash = inner_puzzle.get_tree_hash()
                pubkey, private = await self.wallet_state_manager.get_keys(puzzle_hash)
                synthetic_secret_key = calculate_synthetic_secret_key(private, DEFAULT_HIDDEN_PUZZLE_HASH)
                error, conditions, cost = conditions_dict_for_solution(
                    spend.puzzle_reveal.to_program(),
                    spend.solution.to_program(),
                    self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
                )
                if conditions is not None:
                    synthetic_pk = synthetic_secret_key.get_g1()
                    for pk, msg in pkm_pairs_for_conditions_dict(
                        conditions, spend.coin.name(), self.wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA
                    ):
                        try:
                            assert bytes(synthetic_pk) == pk
                            sigs.append(AugSchemeMPL.sign(synthetic_secret_key, msg))
                        except AssertionError:
                            raise ValueError("This spend bundle cannot be signed by the CAT wallet")

        agg_sig = AugSchemeMPL.aggregate(sigs)
        return SpendBundle.aggregate([spend_bundle, SpendBundle([], agg_sig)])

    async def inner_puzzle_for_cat_puzhash(self, cat_hash: bytes32) -> Program:
        record: DerivationRecord = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
            cat_hash
        )
        inner_puzzle: Program = self.standard_wallet.puzzle_for_pk(bytes(record.pubkey))
        return inner_puzzle

    async def convert_puzzle_hash(self, puzzle_hash: bytes32) -> bytes32:
        record: DerivationRecord = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
            puzzle_hash
        )
        if record is None:
            return puzzle_hash
        else:
            return (await self.inner_puzzle_for_cat_puzhash(puzzle_hash)).get_tree_hash()

    async def get_lineage_proof_for_coin(self, coin) -> Optional[LineageProof]:
        return await self.lineage_store.get_lineage_proof(coin.parent_coin_info)

    async def create_tandem_llc_tx(
        self,
        fee: uint64,
        amount_to_claim: uint64,
        announcement_to_assert: Optional[Announcement] = None,
    ) -> Tuple[TransactionRecord, Optional[Announcement]]:
        """
        This function creates a non-CAT transaction to pay fees, contribute funds for issuance, and absorb melt value.
        It is meant to be called in `generate_unsigned_spendbundle` and as such should be called under the
        wallet_state_manager lock
        """
        announcement = None
        if fee > amount_to_claim:
            littlelambocoin_coins = await self.standard_wallet.select_coins(fee)
            origin_id = list(littlelambocoin_coins)[0].name()
            littlelambocoin_tx = await self.standard_wallet.generate_signed_transaction(
                uint64(0),
                (await self.standard_wallet.get_new_puzzlehash()),
                fee=uint64(fee - amount_to_claim),
                coins=littlelambocoin_coins,
                origin_id=origin_id,  # We specify this so that we know the coin that is making the announcement
                negative_change_allowed=False,
                coin_announcements_to_consume={announcement_to_assert} if announcement_to_assert is not None else None,
            )
            assert littlelambocoin_tx.spend_bundle is not None

            message = None
            for spend in littlelambocoin_tx.spend_bundle.coin_spends:
                if spend.coin.name() == origin_id:
                    conditions = spend.puzzle_reveal.to_program().run(spend.solution.to_program()).as_python()
                    for condition in conditions:
                        if condition[0] == ConditionOpcode.CREATE_COIN_ANNOUNCEMENT:
                            message = condition[1]

            assert message is not None
            announcement = Announcement(origin_id, message)
        else:
            littlelambocoin_coins = await self.standard_wallet.select_coins(fee)
            selected_amount = sum([c.amount for c in littlelambocoin_coins])
            littlelambocoin_tx = await self.standard_wallet.generate_signed_transaction(
                uint64(selected_amount + amount_to_claim - fee),
                (await self.standard_wallet.get_new_puzzlehash()),
                coins=littlelambocoin_coins,
                negative_change_allowed=True,
                coin_announcements_to_consume={announcement_to_assert} if announcement_to_assert is not None else None,
            )
            assert littlelambocoin_tx.spend_bundle is not None

        return littlelambocoin_tx, announcement

    async def generate_unsigned_spendbundle(
        self,
        payments: List[Payment],
        fee: uint64 = uint64(0),
        cat_discrepancy: Optional[Tuple[int, Program]] = None,  # (extra_delta, limitations_solution)
        coins: Set[Coin] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
    ) -> Tuple[SpendBundle, Optional[TransactionRecord]]:
        if coin_announcements_to_consume is not None:
            coin_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in coin_announcements_to_consume}
        else:
            coin_announcements_bytes = None

        if puzzle_announcements_to_consume is not None:
            puzzle_announcements_bytes: Optional[Set[bytes32]] = {a.name() for a in puzzle_announcements_to_consume}
        else:
            puzzle_announcements_bytes = None

        if cat_discrepancy is not None:
            extra_delta, limitations_solution = cat_discrepancy
        else:
            extra_delta, limitations_solution = 0, Program.to([])
        payment_amount: int = sum([p.amount for p in payments])
        starting_amount: int = payment_amount - extra_delta

        if coins is None:
            cat_coins = await self.select_coins(uint64(starting_amount))
        else:
            cat_coins = coins

        selected_cat_amount = sum([c.amount for c in cat_coins])
        assert selected_cat_amount >= starting_amount

        # Figure out if we need to absorb/melt some LLC as part of this
        regular_littlelambocoin_to_claim: int = 0
        if payment_amount > starting_amount:
            fee = uint64(fee + payment_amount - starting_amount)
        elif payment_amount < starting_amount:
            regular_littlelambocoin_to_claim = payment_amount

        need_littlelambocoin_transaction = (fee > 0 or regular_littlelambocoin_to_claim > 0) and (fee - regular_littlelambocoin_to_claim != 0)

        # Calculate standard puzzle solutions
        change = selected_cat_amount - starting_amount
        primaries: List[AmountWithPuzzlehash] = []
        for payment in payments:
            primaries.append({"puzzlehash": payment.puzzle_hash, "amount": payment.amount, "memos": payment.memos})

        if change > 0:
            changepuzzlehash = await self.get_new_inner_hash()
            primaries.append({"puzzlehash": changepuzzlehash, "amount": uint64(change), "memos": []})

        limitations_program_reveal = Program.to([])
        if self.cat_info.my_tail is None:
            assert cat_discrepancy is None
        elif cat_discrepancy is not None:
            limitations_program_reveal = self.cat_info.my_tail

        # Loop through the coins we've selected and gather the information we need to spend them
        spendable_cat_list = []
        littlelambocoin_tx = None
        first = True
        announcement: Announcement
        for coin in cat_coins:
            if first:
                first = False
                announcement = Announcement(coin.name(), std_hash(b"".join([c.name() for c in cat_coins])), b"\xca")
                if need_littlelambocoin_transaction:
                    if fee > regular_littlelambocoin_to_claim:
                        littlelambocoin_tx, _ = await self.create_tandem_llc_tx(
                            fee, uint64(regular_littlelambocoin_to_claim), announcement_to_assert=announcement
                        )
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert=coin_announcements_bytes,
                            puzzle_announcements_to_assert=puzzle_announcements_bytes,
                        )
                    elif regular_littlelambocoin_to_claim > fee:
                        littlelambocoin_tx, _ = await self.create_tandem_llc_tx(fee, uint64(regular_littlelambocoin_to_claim))
                        innersol = self.standard_wallet.make_solution(
                            primaries=primaries,
                            coin_announcements={announcement.message},
                            coin_announcements_to_assert={announcement.name()},
                        )
                else:
                    innersol = self.standard_wallet.make_solution(
                        primaries=primaries,
                        coin_announcements={announcement.message},
                        coin_announcements_to_assert=coin_announcements_bytes,
                        puzzle_announcements_to_assert=puzzle_announcements_bytes,
                    )
            else:
                innersol = self.standard_wallet.make_solution(
                    primaries=[],
                    coin_announcements_to_assert={announcement.name()},
                )
            inner_puzzle = await self.inner_puzzle_for_cat_puzhash(coin.puzzle_hash)
            lineage_proof = await self.get_lineage_proof_for_coin(coin)
            assert lineage_proof is not None
            new_spendable_cat = SpendableCAT(
                coin,
                self.cat_info.limitations_program_hash,
                inner_puzzle,
                innersol,
                limitations_solution=limitations_solution,
                extra_delta=extra_delta,
                lineage_proof=lineage_proof,
                limitations_program_reveal=limitations_program_reveal,
            )
            spendable_cat_list.append(new_spendable_cat)

        cat_spend_bundle = unsigned_spend_bundle_for_spendable_cats(CAT_MOD, spendable_cat_list)
        littlelambocoin_spend_bundle = SpendBundle([], G2Element())
        if littlelambocoin_tx is not None and littlelambocoin_tx.spend_bundle is not None:
            littlelambocoin_spend_bundle = littlelambocoin_tx.spend_bundle

        return (
            SpendBundle.aggregate(
                [
                    cat_spend_bundle,
                    littlelambocoin_spend_bundle,
                ]
            ),
            littlelambocoin_tx,
        )

    async def generate_signed_transaction(
        self,
        amounts: List[uint64],
        puzzle_hashes: List[bytes32],
        fee: uint64 = uint64(0),
        coins: Set[Coin] = None,
        ignore_max_send_amount: bool = False,
        memos: Optional[List[List[bytes]]] = None,
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
    ) -> List[TransactionRecord]:
        if memos is None:
            memos = [[] for _ in range(len(puzzle_hashes))]

        if not (len(memos) == len(puzzle_hashes) == len(amounts)):
            raise ValueError("Memos, puzzle_hashes, and amounts must have the same length")

        payments = []
        for amount, puzhash, memo_list in zip(amounts, puzzle_hashes, memos):
            memos_with_hint: List[bytes] = [puzhash]
            memos_with_hint.extend(memo_list)
            payments.append(Payment(puzhash, amount, memos_with_hint))

        payment_sum = sum([p.amount for p in payments])
        if not ignore_max_send_amount:
            max_send = await self.get_max_send_amount()
            if payment_sum > max_send:
                raise ValueError(f"Can't send more than {max_send} in a single transaction")

        unsigned_spend_bundle, littlelambocoin_tx = await self.generate_unsigned_spendbundle(
            payments,
            fee,
            coins=coins,
            coin_announcements_to_consume=coin_announcements_to_consume,
            puzzle_announcements_to_consume=puzzle_announcements_to_consume,
        )
        spend_bundle = await self.sign(unsigned_spend_bundle)

        # TODO add support for array in stored records
        tx_list = [
            TransactionRecord(
                confirmed_at_height=uint32(0),
                created_at_time=uint64(int(time.time())),
                to_puzzle_hash=puzzle_hashes[0],
                amount=uint64(payment_sum),
                fee_amount=fee,
                confirmed=False,
                sent=uint32(0),
                spend_bundle=spend_bundle,
                additions=spend_bundle.additions(),
                removals=spend_bundle.removals(),
                wallet_id=self.id(),
                sent_to=[],
                trade_id=None,
                type=uint32(TransactionType.OUTGOING_TX.value),
                name=spend_bundle.name(),
                memos=list(compute_memos(spend_bundle).items()),
            )
        ]

        if littlelambocoin_tx is not None:
            tx_list.append(
                TransactionRecord(
                    confirmed_at_height=littlelambocoin_tx.confirmed_at_height,
                    created_at_time=littlelambocoin_tx.created_at_time,
                    to_puzzle_hash=littlelambocoin_tx.to_puzzle_hash,
                    amount=littlelambocoin_tx.amount,
                    fee_amount=littlelambocoin_tx.fee_amount,
                    confirmed=littlelambocoin_tx.confirmed,
                    sent=littlelambocoin_tx.sent,
                    spend_bundle=None,
                    additions=littlelambocoin_tx.additions,
                    removals=littlelambocoin_tx.removals,
                    wallet_id=littlelambocoin_tx.wallet_id,
                    sent_to=littlelambocoin_tx.sent_to,
                    trade_id=littlelambocoin_tx.trade_id,
                    type=littlelambocoin_tx.type,
                    name=littlelambocoin_tx.name,
                    memos=[],
                )
            )

        return tx_list

    async def add_lineage(self, name: bytes32, lineage: Optional[LineageProof], in_transaction):
        """
        Lineage proofs are stored as a list of parent coins and the lineage proof you will need if they are the
        parent of the coin you are trying to spend. 'If I'm your parent, here's the info you need to spend yourself'
        """
        self.log.info(f"Adding parent {name}: {lineage}")
        if lineage is not None:
            await self.lineage_store.add_lineage_proof(name, lineage, in_transaction)

    async def remove_lineage(self, name: bytes32):
        self.log.info(f"Removing parent {name} (probably had a non-CAT parent)")
        await self.lineage_store.remove_lineage_proof(name)

    async def save_info(self, cat_info: CATInfo, in_transaction):
        self.cat_info = cat_info
        current_info = self.wallet_info
        data_str = bytes(cat_info).hex()
        wallet_info = WalletInfo(current_info.id, current_info.name, current_info.type, data_str)
        self.wallet_info = wallet_info
        await self.wallet_state_manager.user_store.update_wallet(wallet_info, in_transaction)
