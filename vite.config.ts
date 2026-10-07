import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Monaco is large and only needed inside an active examination. Splitting
    // it keeps the login and faculty workspaces fast to load.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: 'monaco', test: /node_modules[\\/]@monaco-editor|node_modules[\\/]monaco-editor/ },
            { name: 'react-vendor', test: /node_modules[\\/](react|react-dom|react-router)/ },
          ],
        },
      },
    },
  },
})