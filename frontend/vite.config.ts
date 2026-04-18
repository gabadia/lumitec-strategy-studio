import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8089',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        // Disable response buffering so SSE EventSource streams arrive immediately
        configure: (proxy) => {
          proxy.on('proxyReq', (_proxyReq, req) => {
            if (req.headers.accept?.includes('text/event-stream')) {
              _proxyReq.setHeader('Connection', 'keep-alive')
            }
          })
        },
      },
    },
  },
})
