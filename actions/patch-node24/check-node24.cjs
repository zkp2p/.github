const [major, minor, patch] = process.versions.node.split('.').map(Number);
const patched = major === 24 && Number.isInteger(patch) && (minor > 22 || (minor === 22 && patch >= 3));
console.log(patched && require('node:zlib').constants.BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH === -17);
