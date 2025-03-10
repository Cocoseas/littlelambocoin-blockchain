import React from 'react';
import { Trans } from '@lingui/macro';
import {
  Card,
  FormatBytes,
  Loading,
  Table,
} from '@littlelambocoin/core';
import { Tooltip } from '@material-ui/core';
import { Connection, ServiceConnectionName } from '@littlelambocoin/api';
import { useGetWalletConnectionsQuery } from '@littlelambocoin/api-react';

const cols = [
  {
    minWidth: '200px',
    field(row: Connection) {
      return (
        <Tooltip title={row.nodeId}>
          <span>{row.nodeId}</span>
        </Tooltip>
      );
    },
    title: <Trans>Node ID</Trans>,
  },
  {
    field: 'peerHost',
    title: <Trans>IP address</Trans>,
  },
  {
    field(row: Connection) {
      return `${row.peerPort}/${row.peerServerPort}`;
    },
    title: <Trans>Port</Trans>,
  },
  {
    field(row: Connection) {
      return (
        <>
          <FormatBytes
            value={row.bytesWritten}
            unit="MiB"
            removeUnit
            fixedDecimals
          />
          /
          <FormatBytes
            value={row.bytesRead}
            unit="MiB"
            removeUnit
            fixedDecimals
          />
        </>
      );
    },
    title: <Trans>MiB Up/Down</Trans>,
  },
  {
    field(row: Connection) {
      // @ts-ignore
      return ServiceConnectionName[row.type];
    },
    title: <Trans>Connection type</Trans>,
  },
];

type Props = {
  walletId: number;
};

export default function WalletConnections(props: Props) {
  const { walletId } = props;
  const { data: connections, isLoading } = useGetWalletConnectionsQuery({
    walletId,
  });

  return (
    <Card
      title={<Trans>Connections</Trans>}
    >
      {isLoading ? (
        <Loading center />
      ) : !connections?.length ? (
        <Trans>List of connections is empty</Trans>
      ) : (
        <Table cols={cols} rows={connections} />
      )}
    </Card>
  );
}
