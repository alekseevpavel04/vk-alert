import { expect, test } from 'claude-code/testing'
import { hmacHex } from '../hooks/crypto.js'

test('HMAC совпадает с hmac из Python', async () => {
  const key = new Uint8Array(32).fill(0x0f)
  expect(await hmacHex(key, 'req\nGET\n/health\n1700000000\nабв')).toBe('9cd718aabb56f77472038f355d83f530568f03da31b6591e91d7afe3e8cd71de')
})
