from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from math import hypot
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class SafetyZone:
    zone_id:str
    center_x:float
    center_y:float
    radius_m:float
    activity:str
    window:Window
    severity:int=1

@dataclass(frozen=True)
class VehicleTrack:
    vehicle_id:str
    at:datetime
    x:float
    y:float
    speed_kph:float
    heading_deg:float
    authorized_zones:frozenset[str]=frozenset()
    def __post_init__(self):
        object.__setattr__(self,"at",utc(self.at))

@dataclass(frozen=True)
class SafetyAlert:
    vehicle_id:str
    zone_id:str
    at:datetime
    code:str
    distance_m:float
    predicted:bool=False

class ApronSafetyMonitor:
    def __init__(self,zones:Iterable[SafetyZone]=()):
        self.zones={z.zone_id:z for z in zones}
        self.tracks:dict[str,list[VehicleTrack]]={}
        self.alerts:list[SafetyAlert]=[]

    def set_zone(self,zone:SafetyZone)->None:
        self.zones[zone.zone_id]=zone

    def close_zone(self,zone_id:str)->None:
        self.zones.pop(zone_id,None)

    def ingest(self,track:VehicleTrack,predict_seconds:int=20)->tuple[SafetyAlert,...]:
        rows=self.tracks.setdefault(track.vehicle_id,[])
        if rows and track.at<rows[-1].at:raise ValueError("out-of-order vehicle track")
        rows.append(track)
        generated=[]
        speed_m_s=track.speed_kph/3.6
        from math import radians,sin,cos
        dx=sin(radians(track.heading_deg))*speed_m_s*predict_seconds
        dy=cos(radians(track.heading_deg))*speed_m_s*predict_seconds
        for zone in self.zones.values():
            if not zone.window.contains(track.at):continue
            distance=hypot(track.x-zone.center_x,track.y-zone.center_y)
            if distance<=zone.radius_m and zone.zone_id not in track.authorized_zones:
                generated.append(SafetyAlert(track.vehicle_id,zone.zone_id,track.at,"zone-intrusion",distance,False))
                continue
            future_distance=hypot(track.x+dx-zone.center_x,track.y+dy-zone.center_y)
            if future_distance<=zone.radius_m and zone.zone_id not in track.authorized_zones:
                generated.append(SafetyAlert(track.vehicle_id,zone.zone_id,track.at,"predicted-intrusion",future_distance,True))
        self.alerts.extend(generated)
        return tuple(generated)

    def near_miss(self,a:VehicleTrack,b:VehicleTrack,horizon_seconds:int=15,min_separation_m:float=8)->Decision:
        if abs((a.at-b.at).total_seconds())>5:return Decision(False,"asynchronous","tracks too far apart in time")
        from math import radians,sin,cos
        av=(sin(radians(a.heading_deg))*a.speed_kph/3.6,cos(radians(a.heading_deg))*a.speed_kph/3.6)
        bv=(sin(radians(b.heading_deg))*b.speed_kph/3.6,cos(radians(b.heading_deg))*b.speed_kph/3.6)
        rx=a.x-b.x;ry=a.y-b.y;vx=av[0]-bv[0];vy=av[1]-bv[1]
        denom=vx*vx+vy*vy
        t=0.0 if denom==0 else max(0.0,min(float(horizon_seconds),-(rx*vx+ry*vy)/denom))
        sep=hypot(rx+vx*t,ry+vy*t)
        if sep<min_separation_m:return Decision(False,"near-miss","predicted vehicle separation below threshold",{"seconds":t,"separation_m":sep})
        return Decision(True,"clear","vehicle trajectories remain separated",{"minimum_m":sep})
