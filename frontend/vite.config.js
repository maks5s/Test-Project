import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const apiProxy = 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': apiProxy,
      '/mock': apiProxy,
      '/health': apiProxy,
      '/docs': apiProxy,
      '/openapi.json': apiProxy,
    },
  },
})
