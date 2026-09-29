from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from math import sin,cos,radians,sqrt
from typing import Iterable,Mapping
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class WeatherObservation:
    station:str
    observed_at:datetime
    wind_dir_deg:int|None
    wind_speed_kt:float
    gust_kt:float|None
    visibility_m:int
    ceiling_ft:int|None
    temperature_c:float
    lightning_nm:float|None=None
    braking_action:str="good"
    def __post_init__(self):
        object.__setattr__(self,"observed_at",utc(self.observed_at))
        if self.wind_dir_deg is not None and not 0<=self.wind_dir_deg<360:raise ValueError("invalid wind direction")

@dataclass(frozen=True)
class AircraftWeatherLimits:
    max_crosswind_kt:float
    max_tailwind_kt:float
    min_visibility_m:int
    min_ceiling_ft:int|None
    lightning_suspend_nm:float
    contaminated_crosswind_factor:float=0.7

class WeatherService:
    def __init__(self,max_age_min:int=30):
        self.max_age_min=max_age_min
        self._obs:dict[str,list[WeatherObservation]]={}

    def ingest(self,obs:WeatherObservation)->None:
        rows=self._obs.setdefault(obs.station,[])
        if any(x.observed_at==obs.observed_at for x in rows):
            rows[:]=[x for x in rows if x.observed_at!=obs.observed_at]
        rows.append(obs);rows.sort(key=lambda x:x.observed_at)

    def latest(self,station:str,at:datetime)->WeatherObservation:
        t=utc(at)
        rows=[x for x in self._obs.get(station,()) if x.observed_at<=t]
        if not rows:raise RuntimeError("no weather observation")
        obs=rows[-1]
        if t-obs.observed_at>timedelta(minutes=self.max_age_min):raise RuntimeError("weather observation stale")
        return obs

    def wind_components(self,obs:WeatherObservation,runway_heading_deg:int)->tuple[float,float]:
        if obs.wind_dir_deg is None:return 0.0,0.0
        speed=max(obs.wind_speed_kt,obs.gust_kt or 0.0)
        angle=radians((obs.wind_dir_deg-runway_heading_deg)%360)
        head=speed*cos(angle)
        cross=speed*sin(angle)
        tail=max(0.0,-head)
        return tail,abs(cross)

    def operation(self,station:str,at:datetime,runway_heading_deg:int,limits:AircraftWeatherLimits)->Decision:
        obs=self.latest(station,at)
        tail,cross=self.wind_components(obs,runway_heading_deg)
        cross_limit=limits.max_crosswind_kt
        if obs.braking_action in {"poor","nil"}:cross_limit*=limits.contaminated_crosswind_factor
        if tail>limits.max_tailwind_kt:
            return Decision(False,"tailwind","tailwind limit exceeded",{"tailwind_kt":tail})
        if cross>cross_limit:
            return Decision(False,"crosswind","crosswind limit exceeded",{"crosswind_kt":cross,"limit":cross_limit})
        if obs.visibility_m<limits.min_visibility_m:
            return Decision(False,"visibility","visibility below minimum",{"visibility_m":obs.visibility_m})
        if limits.min_ceiling_ft is not None and obs.ceiling_ft is not None and obs.ceiling_ft<limits.min_ceiling_ft:
            return Decision(False,"ceiling","ceiling below minimum",{"ceiling_ft":obs.ceiling_ft})
        if obs.lightning_nm is not None and obs.lightning_nm<=limits.lightning_suspend_nm:
            return Decision(False,"lightning","ramp activity suspended for lightning",{"distance_nm":obs.lightning_nm})
        return Decision(True,"ok","weather within operating limits",{"tailwind_kt":tail,"crosswind_kt":cross})

    def trend(self,station:str,end:datetime,minutes:int=60)->Mapping[str,float]:
        t=utc(end);start=t-timedelta(minutes=minutes)
        rows=[x for x in self._obs.get(station,()) if start<=x.observed_at<=t]
        if len(rows)<2:raise RuntimeError("insufficient observations")
        duration=max(1,(rows[-1].observed_at-rows[0].observed_at).total_seconds()/60)
        return {
            "temperature_c_per_hour":(rows[-1].temperature_c-rows[0].temperature_c)*60/duration,
            "visibility_m_per_hour":(rows[-1].visibility_m-rows[0].visibility_m)*60/duration,
            "wind_kt_per_hour":(rows[-1].wind_speed_kt-rows[0].wind_speed_kt)*60/duration,
        }
