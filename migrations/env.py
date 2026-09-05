from alembic import context
from sqlalchemy import create_engine, pool

from easylearn.assets import Asset  # noqa: F401
from easylearn.database import Base
from easylearn.documents import models as document_models  # noqa: F401
from easylearn.idempotency import IdempotencyRecord  # noqa: F401
from easylearn.jobs import models as job_models  # noqa: F401
from easylearn.parses import models as parse_models  # noqa: F401
from easylearn.uploads import models as upload_models  # noqa: F401

url = context.config.get_main_option("sqlalchemy.url")
if not url:
    from sqlalchemy.engine import make_url

    from easylearn.config import Settings

    url = (
        make_url(Settings().database_url.get_secret_value())
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
