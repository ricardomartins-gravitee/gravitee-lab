// Change string values only: BSON identities, numbers, dates and binary credentials stay intact.
const ports = JSON.parse(process.env.GRAVITEE_LAB_CLONE_PORTS);
const local = /\b(https?|wss?):\/\/(localhost|127\.0\.0\.1|\[::1\]|host\.docker\.internal|gateway\.docker\.internal)(?::(\d+))?(?=$|[/?#\s"'<>\\)])/gi;
function relocate(value, path) {
  return value.replace(local, (url, scheme, host, port, offset) => {
    // A Gravitee endpoint target on loopback calls its own container, not the host.
    // Definitions may be JSON strings: inspect the immediate key without parsing
    // and reserializing unrelated numbers or credentials in that definition.
    const target = path[path.length - 1] === 'target' || /"target"\s*:\s*"$/.test(value.slice(0, offset));
    if (target && /^(localhost|127\.0\.0\.1|\[::1\])$/i.test(host)) return url;
    const original = port || (/s$/i.test(scheme) ? '443' : '80');
    return Object.hasOwn(ports, original) ? `${scheme}://${host}:${ports[original]}` : url;
  });
}
function changes(value, path, result) {
  if (typeof value === 'string') {
    const next = relocate(value, path);
    if (next !== value) {
      if (path.some(key => key.includes('.') || key.startsWith('$'))) {
        throw new Error('A local URL has a field name that cannot be relocated safely');
      }
      result[path.join('.')] = next;
    }
  } else if (Array.isArray(value)) {
    value.forEach((entry, index) => changes(entry, [...path, String(index)], result));
  } else if (value && Object.prototype.toString.call(value) === '[object Object]' && !value._bsontype) {
    for (const [key, entry] of Object.entries(value)) {
      if (key !== '_id') changes(entry, [...path, key], result);
    }
  }
}
for (const name of db.adminCommand({listDatabases: 1, nameOnly: true}).databases.map(row => row.name)) {
  if (['admin', 'config', 'local'].includes(name)) continue;
  const database = db.getSiblingDB(name);
  for (const info of database.getCollectionInfos({type: 'collection'})) {
    if (info.name.startsWith('system.')) continue;
    const collection = database.getCollection(info.name);
    collection.find().forEach(document => {
      const updates = Object.create(null);
      changes(document, [], updates);
      if (Object.keys(updates).length) {
        const result = collection.updateOne({_id: document._id}, {$set: updates});
        if (!result.acknowledged || result.matchedCount !== 1) throw new Error('A copied document could not be relocated');
      }
    });
  }
}
