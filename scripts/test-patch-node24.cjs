const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const source = readFileSync(join(__dirname, '../actions/patch-node24/check-node24.cjs'), 'utf8');

for (const [version, brotli, expected] of [
  ['24.22.1', -17, false],
  ['24.22.2', -17, false],
  ['24.22.3', -17, true],
  ['24.22.4', -17, true],
  ['24.23.0', -17, true],
  ['24.22.3', -16, false],
  ['22.23.5', -17, false],
  ['26.13.1', -17, false],
  ['24.22.3-rc.1', -17, false],
  ['24.23.0-rc.1', -17, false],
]) {
  test(`Node ${version}, Brotli ${brotli}: ${expected}`, () => {
    const output = [];
    runInNewContext(source, {
      process: { versions: { node: version } },
      require(name) {
        assert.equal(name, 'node:zlib');
        return { constants: { BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH: brotli } };
      },
      console: { log(value) { output.push(value); } },
    });
    assert.deepEqual(output, [expected]);
  });
}
