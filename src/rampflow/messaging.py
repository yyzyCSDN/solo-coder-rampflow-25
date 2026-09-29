from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from collections import deque
from typing import Mapping,Iterable
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class OpsMessage:
    message_id:str
    topic:str
    key:str
    version:int
    emitted_at:datetime
    payload:Mapping[str,object]
    idempotency_key:str
    def __post_init__(self):
        object.__setattr__(self,"emitted_at",utc(self.emitted_at))

@dataclass(frozen=True)
class Delivery:
    subscriber:str
    message:OpsMessage
    attempts:int
    next_attempt:datetime
    acknowledged:bool=False

class MessageBus:
    def __init__(self,retention:int=5000,max_attempts:int=5):
        self.retention=retention;self.max_attempts=max_attempts
        self.messages:deque[OpsMessage]=deque(maxlen=retention)
        self._idempotency:set[str]=set()
        self._versions:dict[tuple[str,str],int]={}
        self.subscribers:dict[str,set[str]]={}
        self.deliveries:dict[tuple[str,str],Delivery]={}

    def subscribe(self,subscriber:str,topics:Iterable[str])->None:
        self.subscribers[subscriber]=set(topics)

    def publish(self,message:OpsMessage)->Decision:
        if message.idempotency_key in self._idempotency:return Decision(True,"duplicate","idempotent publish already accepted")
        key=(message.topic,message.key)
        current=self._versions.get(key,-1)
        if message.version<=current:return Decision(False,"stale","message version not newer",{"current":current})
        self._versions[key]=message.version;self._idempotency.add(message.idempotency_key);self.messages.append(message)
        for subscriber,topics in self.subscribers.items():
            if message.topic in topics or "*" in topics:
                self.deliveries[(subscriber,message.message_id)]=Delivery(subscriber,message,0,message.emitted_at)
        return Decision(True,"published","message published",{"subscribers":sum(1 for t in self.subscribers.values() if message.topic in t or "*" in t)})

    def due(self,subscriber:str,at:datetime,limit:int=100)->tuple[Delivery,...]:
        t=utc(at)
        rows=[d for (s,_),d in self.deliveries.items() if s==subscriber and not d.acknowledged and d.next_attempt<=t and d.attempts<self.max_attempts]
        rows.sort(key=lambda d:(d.next_attempt,d.message.emitted_at,d.message.message_id))
        return tuple(rows[:limit])

    def mark_attempt(self,subscriber:str,message_id:str,at:datetime,success:bool)->Delivery:
        key=(subscriber,message_id);d=self.deliveries[key];t=utc(at)
        if success:
            updated=replace(d,attempts=d.attempts+1,acknowledged=True,next_attempt=t)
        else:
            attempts=d.attempts+1
            backoff=min(300,2**attempts)
            updated=replace(d,attempts=attempts,next_attempt=t+timedelta(seconds=backoff))
        self.deliveries[key]=updated
        return updated

    def dead_letters(self)->tuple[Delivery,...]:
        rows=[d for d in self.deliveries.values() if not d.acknowledged and d.attempts>=self.max_attempts]
        return tuple(sorted(rows,key=lambda d:(d.subscriber,d.message.message_id)))
