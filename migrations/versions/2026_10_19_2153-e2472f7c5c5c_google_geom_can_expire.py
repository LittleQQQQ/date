"""Google 餐廳的座標可以清掉（規格書 4.5）：座標最多存 30 天，更新不到就清掉並設為無效。

restaurants.geom 改成可以是 null；另外加上 CHECK：OSM 來源一定要有座標（Alembic 不會自動偵測 CHECK，手動寫）。
退回時，沒有座標的 Google 餐廳會被刪掉（它們本來就是無效的），否則改回 NOT NULL 會失敗。

Revision ID: e2472f7c5c5c
Revises: 804537506c0c
Create Date: 2026-10-19 21:53:19.099326+08:00

"""
from typing import Sequence, Union

import geoalchemy2
from alembic import op

revision: str = 'e2472f7c5c5c'
down_revision: Union[str, Sequence[str], None] = '804537506c0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GEOM = geoalchemy2.types.Geography(geometry_type='POINT', srid=4326, spatial_index=False)


def upgrade() -> None:
    op.alter_column('restaurants', 'geom', existing_type=GEOM, nullable=True)
    op.create_check_constraint(op.f('ck_restaurants_osm_has_geom'), 'restaurants', "source <> 'osm' OR geom IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint(op.f('ck_restaurants_osm_has_geom'), 'restaurants', type_='check')
    op.execute("DELETE FROM restaurants WHERE geom IS NULL")
    op.alter_column('restaurants', 'geom', existing_type=GEOM, nullable=False)
