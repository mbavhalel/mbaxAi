import { defineConfig, loadEnv } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const key = (env.VITE_SUPABASE_PUBLISHABLE_KEY || '').trim();
  let role;
  try { role = JSON.parse(Buffer.from(key.split('.')[1] || '', 'base64url').toString()).role; } catch { /* Publishable keys are not JWTs. */ }
  if (key.startsWith('sb_secret_') || role === 'service_role') {
    throw new Error('VITE_SUPABASE_PUBLISHABLE_KEY must be a public publishable/anon key. Never use a Supabase secret or service_role key in the website.');
  }
  return {
    base: './',
    build: {
      rollupOptions: {
        input: {
          index: fileURLToPath(new URL('./index.html', import.meta.url)),
          dashboard: fileURLToPath(new URL('./dashboard.html', import.meta.url)),
        },
      },
    },
  };
});
