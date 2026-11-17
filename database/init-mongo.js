// Creates the experiment database and its collections on first start.
// (The backend also ensures indexes at startup, so this is idempotent.)
const dbName = process.env.MONGO_INITDB_DATABASE || "human_ai_experiment";
const d = db.getSiblingDB(dbName);
["sessions", "messages", "votes", "results"].forEach((c) => {
  if (!d.getCollectionNames().includes(c)) d.createCollection(c);
});
d.messages.createIndex({ session_id: 1, ts: 1 });
d.votes.createIndex({ session_id: 1, round: 1, voter: 1 }, { unique: true });
d.results.createIndex({ session_id: 1, round: 1 });
