// Exercise the actual mongosh script with BSON-like values and a recording collection.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const script = readFileSync(resolve(__dirname, '../clone/mongodb.js'), 'utf8');

function run(document, result = {acknowledged: true, matchedCount: 1}) {
  const updates = [];
  const collection = {find: () => [document], updateOne: (filter, value) => {
    updates.push({filter, value}); return result;
  }};
  const db = {adminCommand: () => ({databases: [{name: 'admin'}, {name: 'gravitee'}]}),
    getSiblingDB: () => ({getCollectionInfos: () => [{name: 'applications'}, {name: 'system.views'}],
      getCollection: () => collection})};
  vm.runInNewContext(script, {db, process: {env: {GRAVITEE_LAB_CLONE_PORTS: '{"8082":49001,"49001":49002}'}}});
  return updates;
}

test('nested URLs and serialized definitions relocate once; external addresses and BSON stay intact', () => {
  const document = {_id: 'http://localhost:8082/identity', nested: {callbacks: [
    'http://localhost:8082/callback?q=1#fragment', 'https://127.0.0.1:8082/path',
    'wss://[::1]:8082/socket', 'http://host.docker.internal:49001/path',
    'http://gateway.docker.internal:8082/path', 'https://external.test:8082/path',
    'http://localhost:80820/path', 'http://apim-gateway:8082/path']},
    definition: '{"url":"http://localhost:8082/api","target":"http://localhost:8082/self","big":9223372036854775806}', count: 8082,
    endpoint: {target: 'http://localhost:8082/self'},
    binary: {_bsontype: 'Binary', value: 'http://localhost:8082/credential'}, date: new Date()};
  const before = JSON.stringify(document);
  const updates = run(document);
  assert.equal(JSON.stringify(document), before);
  assert.equal(updates.length, 1);
  assert.equal(updates[0].filter._id, document._id);
  const set = updates[0].value.$set;
  assert.equal(set['nested.callbacks.0'], 'http://localhost:49001/callback?q=1#fragment');
  assert.equal(set['nested.callbacks.1'], 'https://127.0.0.1:49001/path');
  assert.equal(set['nested.callbacks.2'], 'wss://[::1]:49001/socket');
  assert.equal(set['nested.callbacks.3'], 'http://host.docker.internal:49002/path');
  assert.equal(set['nested.callbacks.4'], 'http://gateway.docker.internal:49001/path');
  assert.equal(set.definition, '{"url":"http://localhost:49001/api","target":"http://localhost:8082/self","big":9223372036854775806}');
  assert.equal(Object.keys(set).length, 6);
});
test('a database without source-local URLs receives no write', () => {
  assert.equal(run({_id: 'id', endpoint: 'https://external.test:8082', secret: 'opaque'}).length, 0);
});
test('a failed write or unsafe field path fails the clone', () => {
  assert.throws(() => run({_id: 'id', url: 'http://localhost:8082'}, {acknowledged: true, matchedCount: 0}), /could not be relocated/);
  assert.throws(() => run({_id: 'id', 'a.b': 'http://localhost:8082'}), /field name/);
});
