/* SPDX-License-Identifier: GPL-3.0-or-later */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const babel = require('@babel/core');
const acorn = require('acorn');
const directory = path.resolve(__dirname, '..', 'static');
const source = fs.readFileSync(path.resolve(__dirname, '..', '..', 'static', 'js', 'libs', 'epub.min.js'));
const hash = crypto.createHash('sha256').update(source).digest('hex');
if (hash !== '06eae15745107b4aa508c95538275251f69bfb9f1175621fc458d9f42ed082d4') {
  throw new Error('Vendored EPUB source changed; revalidate the IE11 patches before rebuilding');
}
let code = babel.transformSync(source.toString(), {
  presets: [[require.resolve('@babel/preset-env'), {targets: {ie: '11'}, modules: false}]],
  comments: true, compact: true, babelrc: false, configFile: false
}).code;
const original = '"undefined"==typeof XMLSerializer||e?d.DOMParser:XMLSerializer';
if (code.split(original).length !== 2) throw new Error('Expected serializer branch exactly once');
code = code.replace(original, '"undefined"==typeof XMLSerializer||e?d.XMLSerializer:XMLSerializer');
fs.mkdirSync(path.join(directory, 'vendor'), {recursive: true});
fs.writeFileSync(path.join(directory, 'vendor', 'epub.legacy.js'), code);
fs.copyFileSync(require.resolve('core-js-bundle/minified.js'), path.join(directory, 'vendor', 'core-js.js'));
fs.copyFileSync(require.resolve('core-js-bundle/LICENSE'), path.join(directory, 'vendor', 'core-js.LICENSE'));
for (const file of ['vendor/epub.legacy.js', 'vendor/core-js.js', 'reader.js', 'immersive.js', 'epub-lifecycle.js']) {
  acorn.parse(fs.readFileSync(path.join(directory, file), 'utf8'), {ecmaVersion: 5});
}
console.log('RT assets built; all browser scripts parse as ES5.');
