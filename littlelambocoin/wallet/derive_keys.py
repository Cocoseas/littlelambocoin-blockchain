from typing import List, Optional, Tuple

from blspy import AugSchemeMPL, PrivateKey, G1Element

from littlelambocoin.util.ints import uint32

# EIP 2334 bls key derivation
# https://eips.ethereum.org/EIPS/eip-2334
# 12381 = bls spec number
# 8444 = Chia blockchain number and port number for compatibility
# 0, 1, 2, 3, 4, 5, 6 farmer, pool, wallet, local, backup key, singleton, pooling authentication key numbers

# Allows up to 100 pool wallets (plot NFTs)
MAX_POOL_WALLETS = 100


def _derive_path(sk: PrivateKey, path: List[int]) -> PrivateKey:
    for index in path:
        sk = AugSchemeMPL.derive_child_sk(sk, index)
    return sk


def _derive_path_unhardened(sk: PrivateKey, path: List[int]) -> PrivateKey:
    for index in path:
        sk = AugSchemeMPL.derive_child_sk_unhardened(sk, index)
    return sk


def master_sk_to_farmer_sk(master: PrivateKey) -> PrivateKey:
    return _derive_path(master, [12381, 8444, 0, 0])


def master_sk_to_pool_sk(master: PrivateKey) -> PrivateKey:
    return _derive_path(master, [12381, 8444, 1, 0])


def master_sk_to_wallet_sk_intermediate(master: PrivateKey) -> PrivateKey:
    return _derive_path(master, [12381, 8444, 2])


def master_sk_to_wallet_sk(master: PrivateKey, index: uint32) -> PrivateKey:
    intermediate = master_sk_to_wallet_sk_intermediate(master)
    return _derive_path(intermediate, [index])


def master_sk_to_wallet_sk_unhardened_intermediate(master: PrivateKey) -> PrivateKey:
    return _derive_path_unhardened(master, [12381, 8444, 2])


def master_sk_to_wallet_sk_unhardened(master: PrivateKey, index: uint32) -> PrivateKey:
    intermediate = master_sk_to_wallet_sk_unhardened_intermediate(master)
    return _derive_path_unhardened(intermediate, [index])


def master_sk_to_local_sk(master: PrivateKey) -> PrivateKey:
    return _derive_path(master, [12381, 8444, 3, 0])


def master_sk_to_backup_sk(master: PrivateKey) -> PrivateKey:
    return _derive_path(master, [12381, 8444, 4, 0])


def master_sk_to_singleton_owner_sk(master: PrivateKey, pool_wallet_index: uint32) -> PrivateKey:
    """
    This key controls a singleton on the blockchain, allowing for dynamic pooling (changing pools)
    """
    return _derive_path(master, [12381, 8444, 5, pool_wallet_index])


def master_sk_to_pooling_authentication_sk(master: PrivateKey, pool_wallet_index: uint32, index: uint32) -> PrivateKey:
    """
    This key is used for the farmer to authenticate to the pool when sending partials
    """
    assert index < 10000
    assert pool_wallet_index < 10000
    return _derive_path(master, [12381, 8444, 6, pool_wallet_index * 10000 + index])


def find_owner_sk(all_sks: List[PrivateKey], owner_pk: G1Element) -> Optional[Tuple[G1Element, uint32]]:
    for pool_wallet_index in range(MAX_POOL_WALLETS):
        for sk in all_sks:
            try_owner_sk = master_sk_to_singleton_owner_sk(sk, uint32(pool_wallet_index))
            if try_owner_sk.get_g1() == owner_pk:
                return try_owner_sk, uint32(pool_wallet_index)
    return None


def find_authentication_sk(all_sks: List[PrivateKey], owner_pk: G1Element) -> Optional[PrivateKey]:
    # NOTE: might need to increase this if using a large number of wallets, or have switched authentication keys
    # many times.
    for pool_wallet_index in range(MAX_POOL_WALLETS):
        for sk in all_sks:
            try_owner_sk = master_sk_to_singleton_owner_sk(sk, uint32(pool_wallet_index))
            if try_owner_sk.get_g1() == owner_pk:
                # NOTE: ONLY use 0 for authentication key index to ensure compatibility
                return master_sk_to_pooling_authentication_sk(sk, uint32(pool_wallet_index), uint32(0))
    return None
