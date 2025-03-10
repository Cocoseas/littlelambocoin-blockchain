import React from 'react';
import { ModeProvider } from '@littlelambocoin/core';
import AppRouter from './AppRouter';

export default function App() {
  return (
    <ModeProvider persist>
      <AppRouter />
    </ModeProvider>
  );
}
