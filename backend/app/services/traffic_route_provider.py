"""이전 import 경로 호환용. 실제 구현은 services.mapf에 한 곳만 둔다."""

from importlib import import_module
import sys

# 같은 모듈 객체를 사용해 설정·로그 경로·시험용 교체가 양쪽에서 일치한다.
sys.modules[__name__] = import_module(".mapf.traffic_route_provider", __package__)
