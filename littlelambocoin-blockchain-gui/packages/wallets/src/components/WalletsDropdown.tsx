import React, { useMemo } from 'react';
import { ListItemIcon, ListItemText, Typography } from '@material-ui/core';
import { Dropdown, Flex, Loading, useTrans } from '@littlelambocoin/core';
import { useGetWalletsQuery } from '@littlelambocoin/api-react';
import { WalletType, type Wallet } from '@littlelambocoin/api';
import { useNavigate } from 'react-router';
import WalletName from '../constants/WalletName';
import WalletIcon from './WalletIcon';
import WalletBadge from './WalletBadge';

function getPrimaryTitle(wallet: Wallet): string {
  switch (wallet.type) {
    case WalletType.STANDARD_WALLET:
      return 'Littlelambocoin';
    default:
      return wallet.name;
  }
}

type Props = {
  walletId: number;
};

export default function WalletsDropdown(props: Props) {
  const { walletId } = props;
  const navigate = useNavigate();
  const trans = useTrans();
  const { data: wallets, isLoading } = useGetWalletsQuery();

  const options = useMemo(() => {
    if (isLoading) {
      return [];
    }

    return wallets
      .filter(wallet => ![WalletType.POOLING_WALLET].includes(wallet.type))
      .map((wallet) => {
        const primaryTitle = getPrimaryTitle(wallet);
        const secondaryTitle = trans(WalletName[wallet.type]);
        const hasSameTitle = primaryTitle.toLowerCase() === secondaryTitle.toLowerCase();

        return {
          wallet,
          value: wallet.id,
          label: (
            <>
              <ListItemIcon>
                <WalletIcon wallet={wallet} />
              </ListItemIcon>
              <ListItemText
                primary={(
                  <Flex gap={1} alignItems="center">
                    <Typography>{primaryTitle}</Typography>
                    <WalletBadge wallet={wallet} fontSize="small" tooltip />
                  </Flex>
                )}
                secondary={!hasSameTitle ? secondaryTitle: undefined}
                secondaryTypographyProps={{
                  variant: 'caption',
                }}
              />
            </>
          ),
        };
      });
  }, [wallets, walletId, isLoading]);

  function handleSelectWallet(walletId: number) {
    navigate(`/dashboard/wallets/${walletId}`);
  }

  if (isLoading) {
    return (
      <Loading size="small" />
    );
  }

  return (
    <Dropdown 
      options={options}
      selected={walletId}
      onSelect={handleSelectWallet}
    >
      {(option) => !!option?.wallet && (
        <Flex gap={1} alignItems="center">
          <Typography>{getPrimaryTitle(option.wallet)}</Typography>
          <WalletBadge wallet={option.wallet} fontSize="small" />
        </Flex>
      )}
    </Dropdown>
  );
}
