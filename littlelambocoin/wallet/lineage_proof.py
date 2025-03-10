from dataclasses import dataclass
from typing import Optional, Any, List

from littlelambocoin.types.blockchain_format.sized_bytes import bytes32
from littlelambocoin.types.blockchain_format.program import Program
from littlelambocoin.util.ints import uint64
from littlelambocoin.util.streamable import Streamable, streamable


@dataclass(frozen=True)
@streamable
class LineageProof(Streamable):
    parent_name: Optional[bytes32] = None
    inner_puzzle_hash: Optional[bytes32] = None
    amount: Optional[uint64] = None

    def to_program(self) -> Program:
        final_list: List[Any] = []
        if self.parent_name is not None:
            final_list.append(self.parent_name)
        if self.inner_puzzle_hash is not None:
            final_list.append(self.inner_puzzle_hash)
        if self.amount is not None:
            final_list.append(self.amount)
        return Program.to(final_list)

    def is_none(self) -> bool:
        return all([self.parent_name is None, self.inner_puzzle_hash is None, self.amount is None])
