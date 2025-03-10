import React from 'react';
import styled from 'styled-components';
import { Box, BoxProps } from '@material-ui/core';
import { Littlelambocoin } from '@littlelambocoin/icons';

const StyledLittlelambocoin = styled(Littlelambocoin)`
  max-width: 100%;
  width: auto;
  height: auto;
`;

export default function Logo(props: BoxProps) {
  return (
    <Box {...props}>
      <StyledLittlelambocoin />
    </Box>
  );
}
