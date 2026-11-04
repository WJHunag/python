// Run the real page helpers with a small DOM fixture; no browser/service/network.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync(path.join(__dirname, '../frontend/dashboard_v2/static/camera_settings.html'), 'utf8');
const input = {value: '', type: 'number', getAttribute: () => 'reference_distance_threshold_px'};
const container = {innerHTML: '', querySelector: () => input};
const document = {
  addEventListener() {},
  getElementById: () => container,
  querySelector: () => null,
  querySelectorAll: selector => selector === '.param-input' ? [input] : [],
};
const context = vm.createContext({document, window: {currentCamera: {NVR_channel: 1}},
  console: {log() {}, warn() {}, error() {}}, sessionStorage: {}, setTimeout});
for (const match of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)) {
  vm.runInContext(match[1], context);
}
context.generateFunctionSwitches({reference_distance_threshold_px: 1200});
assert.strictEqual(input.value, 1200);
assert(container.innerHTML.includes('圖片寬度的 1/3'));
input.value = '200';
assert.strictEqual(context.collectCurrentSettings().function_switches.reference_distance_threshold_px, 200);
input.value = '';
assert.strictEqual(context.collectCurrentSettings().function_switches.reference_distance_threshold_px, null);
for (const value of ['0', '-1', 'Infinity', 'bad']) {
  input.value = value;
  assert.throws(() => context.collectCurrentSettings());
}
console.log('PASS: actual page render, numeric submission, clear-to-auto and validation');
