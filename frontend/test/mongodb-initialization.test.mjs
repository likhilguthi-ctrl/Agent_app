import assert from "node:assert/strict";
import { test } from "node:test";
import { MongoClient } from "mongodb";

const mongodbModuleUrl = new URL("../src/lib/mongodb.ts", import.meta.url);

function resetMongoCache() {
  globalThis._mongoClientPromise = undefined;
  globalThis._mongoSeeded = undefined;
}

test("MongoDB module imports without configuration and rejects access before connecting", async (t) => {
  const previousUri = process.env.MONGODB_URI;
  const previousNodeEnv = process.env.NODE_ENV;
  delete process.env.MONGODB_URI;
  process.env.NODE_ENV = "development";
  resetMongoCache();

  try {
    const { getDb } = await import(mongodbModuleUrl.href);
    let connectCalls = 0;
    t.mock.method(MongoClient.prototype, "connect", async function () {
      connectCalls += 1;
      return this;
    });

    await assert.rejects(getDb(), /MongoDB is not configured\./);
    assert.equal(connectCalls, 0);
  } finally {
    resetMongoCache();
    if (previousUri === undefined) delete process.env.MONGODB_URI;
    else process.env.MONGODB_URI = previousUri;
    if (previousNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previousNodeEnv;
  }
});

test("configured runtime initialization uses MongoClient without opening a real connection", async (t) => {
  const previousUri = process.env.MONGODB_URI;
  const previousDbName = process.env.MONGODB_DB;
  const previousNodeEnv = process.env.NODE_ENV;
  process.env.MONGODB_URI = "mongodb://127.0.0.1:27017/isolated-test";
  process.env.MONGODB_DB = "isolated_test";
  process.env.NODE_ENV = "development";
  resetMongoCache();

  const createdCollections = new Set();
  const indexedCollections = new Set();
  const database = {
    listCollections() {
      return { toArray: async () => [] };
    },
    async createCollection(name) {
      createdCollections.add(name);
    },
    collection(name) {
      return {
        createIndex: async () => indexedCollections.add(name),
        countDocuments: async () => 1,
      };
    },
  };
  const connectedClients = [];
  const selectedDatabases = [];

  try {
    t.mock.method(MongoClient.prototype, "connect", async function () {
      connectedClients.push(this);
      return this;
    });
    t.mock.method(MongoClient.prototype, "db", function (name) {
      selectedDatabases.push(name);
      return database;
    });

    const { COLLECTIONS, getDb } = await import(mongodbModuleUrl.href);
    const result = await getDb();

    assert.equal(result, database);
    assert.equal(connectedClients.length, 1);
    assert.deepEqual(selectedDatabases, ["isolated_test"]);
    assert.deepEqual([...createdCollections].sort(), Object.values(COLLECTIONS).sort());
    assert.ok(indexedCollections.has(COLLECTIONS.packages));
    assert.ok(indexedCollections.has(COLLECTIONS.otps));
  } finally {
    resetMongoCache();
    if (previousUri === undefined) delete process.env.MONGODB_URI;
    else process.env.MONGODB_URI = previousUri;
    if (previousDbName === undefined) delete process.env.MONGODB_DB;
    else process.env.MONGODB_DB = previousDbName;
    if (previousNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previousNodeEnv;
  }
});

test("connection failures do not expose the configured MongoDB URI", async (t) => {
  const previousUri = process.env.MONGODB_URI;
  const previousNodeEnv = process.env.NODE_ENV;
  const uri =
    "mongodb://127.0.0.1:27017/isolated-test?appName=uri-redaction-test";
  process.env.MONGODB_URI = uri;
  process.env.NODE_ENV = "development";
  resetMongoCache();

  try {
    t.mock.method(MongoClient.prototype, "connect", async () => {
      throw new Error(`connection failed for ${uri}`);
    });
    const { getDb } = await import(mongodbModuleUrl.href);

    await assert.rejects(getDb(), (error) => {
      assert.equal(error.message, "Could not connect to MongoDB. Check runtime configuration and database availability.");
      assert.equal(error.message.includes(uri), false);
      return true;
    });
  } finally {
    resetMongoCache();
    if (previousUri === undefined) delete process.env.MONGODB_URI;
    else process.env.MONGODB_URI = previousUri;
    if (previousNodeEnv === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previousNodeEnv;
  }
});
