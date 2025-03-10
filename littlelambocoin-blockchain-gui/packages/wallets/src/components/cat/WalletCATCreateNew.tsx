import React, { useState } from 'react';
import { Trans } from '@lingui/macro';
import { littlelambocoinToMojo, AlertDialog, Amount, Fee, Back, ButtonLoading, Card, Flex, Form } from '@littlelambocoin/core';
import { Box, Grid } from '@material-ui/core';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router';

type CreateCATWalletData = {
  amount: string;
  fee: string;
};

export default function WalletCATCreateNew() {
  const navigate = useNavigate();
  const methods = useForm<CreateCATWalletData>({
    defaultValues: {
      amount: '',
      fee: '',
    },
  });
  const [loading, setLoading] = useState<boolean>(false);
  const [addCATToken, { isLoading: isAddCATTokenLoading }] = useAddCATTokenMutation();

  async function handleSubmit(values: CreateCATWalletData) {
    try {
      const { amount, fee } = values;
      setLoading(true);
      /* fee and amount is optional
      if (//!amount ||
        // Number(amount) === 0 ||
        // !Number(amount) ||
        isNaN(Number(amount))
      ) {
        dispatch(
          openDialog(
            <AlertDialog>
              <Trans>Please enter a valid numeric amount</Trans>
            </AlertDialog>,
          ),
        );
        return;
      }

      if (fee === '' || isNaN(Number(fee))) {
        dispatch(
          openDialog(
            <AlertDialog>
              <Trans>Please enter a valid numeric fee</Trans>
            </AlertDialog>,
          ),
        );
        return;
      }
      */

      const amountMojos = littlelambocoinToMojo(amount || '0');
      const feeMojos = littlelambocoinToMojo(fee || '0');


      /*
      const response = await dispatch(create_cc_action(amountMojos, feeMojos));
      if (response && response.data && response.data.success === true) {
        history.push(`/dashboard/wallets/${response.data.wallet_id}`);
      }
      */
    } finally {
      setLoading(false);
    }
  }

  return (
    <Form methods={methods} onSubmit={handleSubmit}>
      <Flex flexDirection="column" gap={3}>
        <Back variant="h5">
          <Trans>Create New Littlelambocoin Asset Token Wallet</Trans>
        </Back>
        <Card>
          <Grid spacing={2} container>
            <Grid xs={12} md={6} item>
              <Amount
                name="amount"
                variant="outlined"
                fullWidth
              />
            </Grid>
            <Grid xs={12} md={6} item>
              <Fee
                variant="outlined"
                fullWidth
              />
            </Grid>
          </Grid>
        </Card>
        <Box>
          <ButtonLoading
            type="submit"
            variant="contained"
            color="primary"
            loading={loading}
          >
            <Trans>Create</Trans>
          </ButtonLoading>
        </Box>
      </Flex>
    </Form>
  );
}
