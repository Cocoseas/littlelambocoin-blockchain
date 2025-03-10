from typing import List, Dict

from clvm.casts import int_from_bytes
from littlelambocoin.types.blockchain_format.program import INFINITE_COST
from littlelambocoin.types.spend_bundle import SpendBundle
from littlelambocoin.types.blockchain_format.sized_bytes import bytes32
from littlelambocoin.types.blockchain_format.coin import Coin
from littlelambocoin.types.condition_opcodes import ConditionOpcode


def compute_memos(bundle: SpendBundle) -> Dict[bytes32, List[bytes]]:
    """
    Retrieves the memos for additions in this spend_bundle, which are formatted as a list in the 3rd parameter of
    CREATE_COIN. If there are no memos, the addition coin_id is not included. If they are not formatted as a list
    of bytes, they are not included. This is expensive to call, it should not be used in full node code.
    """
    memos: Dict[bytes32, List[bytes]] = {}
    for coin_spend in bundle.coin_spends:
        _, result = coin_spend.puzzle_reveal.run_with_cost(INFINITE_COST, coin_spend.solution)
        for condition in result.as_python():
            if condition[0] == ConditionOpcode.CREATE_COIN and len(condition) >= 4:
                # If only 3 elements (opcode + 2 args), there is no memo, this is ph, amount
                coin_added = Coin(coin_spend.coin.name(), bytes32(condition[1]), int_from_bytes(condition[2]))
                if type(condition[3]) != list:
                    # If it's not a list, it's not the correct format
                    continue
                memos[coin_added.name()] = condition[3]
    return memos
