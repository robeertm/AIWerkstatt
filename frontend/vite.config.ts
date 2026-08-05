import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy so `npm run dev` talks to a locally running backend on :8095.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist' },
  server: { proxy: { '/api': 'http://localhost:8095' } },
})
