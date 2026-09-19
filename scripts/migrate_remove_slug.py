"""
One-time migration: remove `slug` from all existing schools
and drop the unique index on slug.
"""
from pymongo import MongoClient
from config import Config

client = MongoClient(Config.MONGO_URI)
db = client[Config.MONGO_DB_NAME]

print(f"Connected to {Config.MONGO_DB_NAME}")

# 1. Show schools before
total = db.schools.count_documents({})
with_slug = db.schools.count_documents({"slug": {"$exists": True}})
print(f"Schools total: {total} · With slug: {with_slug}")

# 2. Unset the slug field from every school
result = db.schools.update_many(
    {"slug": {"$exists": True}},
    {"$unset": {"slug": ""}}
)
print(f"✓ Unset slug on {result.modified_count} schools")

# 3. Drop the slug index if it exists
indexes = db.schools.index_information()
if "slug_1" in indexes:
    db.schools.drop_index("slug_1")
    print("✓ Dropped index: slug_1")
else:
    print("· No slug_1 index to drop")

# 4. Verify
remaining = db.schools.count_documents({"slug": {"$exists": True}})
print(f"✓ Schools still with slug: {remaining}")
print("Done.")