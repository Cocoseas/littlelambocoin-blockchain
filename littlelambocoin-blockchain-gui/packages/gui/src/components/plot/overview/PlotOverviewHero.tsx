import React from 'react';
import { Trans } from '@lingui/macro';
import { useNavigate } from 'react-router-dom';
import { useRefreshPlotsMutation } from '@littlelambocoin/api-react';
import { Button, Grid, Typography, Divider } from '@material-ui/core';
import { CardHero, Flex, Link, useOpenDialog } from '@littlelambocoin/core';
import { PlotHero as PlotHeroIcon } from '@littlelambocoin/icons';
import PlotAddDirectoryDialog from '../PlotAddDirectoryDialog';

export default function PlotOverviewHero() {
  const navigate = useNavigate();
  const openDialog = useOpenDialog();
  const [refreshPlots] = useRefreshPlotsMutation();

  function handleAddPlot() {
    navigate('/dashboard/plot/add');
  }

  function handleAddPlotDirectory() {
    openDialog(<PlotAddDirectoryDialog />);
  }

  async function handleRefreshPlots() {
    await refreshPlots().unwrap();
  }

  return (
    <Grid container>
      <Grid xs={12} md={6} lg={5} item>
        <CardHero>
          <PlotHeroIcon fontSize="large" />
          <Typography variant="body1">
            <Trans>
              {
                'Plots are allocated space on your hard drive used to farm and earn Littlelambocoin. '
              }
              <Link
                target="_blank"
                href="https://github.com/BTCgreen-Network/littlelambocoin-blockchain/wiki/Network-Architecture"
              >
                Learn more
              </Link>
            </Trans>
          </Typography>
          <Flex gap={1}>
            <Button
              onClick={handleAddPlot}
              variant="contained"
              color="primary"
              fullWidth
            >
              <Trans>Add a Plot</Trans>
            </Button>
            <Button
              onClick={handleRefreshPlots}
              variant="outlined"
              color="primary"
              fullWidth
            >
              <Trans>Refresh Plots</Trans>
            </Button>
          </Flex>

          <Divider />

          <Typography variant="body1">
            <Trans>
              {'Do you have existing plots on this machine? '}
              <Link onClick={handleAddPlotDirectory} variant="body1">
                Add Plot Directory
              </Link>
            </Trans>
          </Typography>
        </CardHero>
      </Grid>
    </Grid>
  );
}
