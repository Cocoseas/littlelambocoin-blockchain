import React from 'react';
import { Trans } from '@lingui/macro';
import WalletCards from '../WalletCards';

type Props = {
  walletId: number;
};

export default function WalletStandardCards(props: Props) {
  const { walletId } = props;

  return (
    <WalletCards
      walletId={walletId}
      totalBalanceTooltip={
        <Trans>
          This is the total amount of littlelambocoin in the blockchain at the current peak
          sub block that is controlled by your private keys. It includes frozen
          farming rewards, but not pending incoming and outgoing transactions.
        </Trans>
      }
      spendableBalanceTooltip={
        <Trans>
          This is the amount of Littlelambocoin that you can currently use to make
          transactions. It does not include pending farming rewards, pending
          incoming transactions, and Littlelambocoin that you have just spent but is not
          yet in the blockchain.
        </Trans>
      }
      pendingTotalBalanceTooltip={
        <Trans>
          This is the total balance + pending balance: it is what your balance
          will be after all pending transactions are confirmed.
        </Trans>
      }
      pendingBalanceTooltip={
        <Trans>
          This is the sum of the incoming and outgoing pending transactions (not
          yet included into the blockchain). This does not include farming
          rewards.
        </Trans>
      }
      pendingChangeTooltip={
        <Trans>
          This is the pending change, which are change coins which you have sent
          to yourself, but have not been confirmed yet.
        </Trans>
      }
    />
  );
}
