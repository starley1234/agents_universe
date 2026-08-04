import type { Config } from 'tailwindcss'
const config: Config = { content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'], theme: { extend: { colors: { graphite: '#0b1020', neon: '#39a7ff', amberMind: '#f7b955', artifact: '#6ee7b7' } } }, plugins: [] }
export default config
