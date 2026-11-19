"""Claude Code MCP channel extension; not a desktop GUI automation mechanism."""
import threading
import sqlite3

class ChannelWatch:
    def __init__(self,dispatch,emit,interval=1):
        self.dispatch=dispatch;self.emit=emit;self.interval=interval
        self.binding=None;self.seen=set();self.lock=threading.Lock();self.stop=threading.Event()
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()
    def bind(self,args):
        self.dispatch('inbox',dict(args,peek=True))  # validate the binding without producing a read receipt
        with self.lock:
            if self.binding and self.binding!=args:
                raise ValueError('This MCP connection is already bound to a different participant/project; reconnect before changing it')
            self.binding=dict(args)
        return {'watching':args['name'],'project_id':args['project_id'],
                'transport':'notifications/claude/channel','delivery_confirmed':False,
                'requires':'Claude Code launched with --dangerously-load-development-channels server:agent-duet; session remains open'}
    def loop(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                binding=dict(self.binding) if self.binding else None
            if not binding:
                continue
            try:
                rows=self.dispatch('inbox',dict(binding,peek=True))['messages']
                for row in rows:
                    if row['id'] in self.seen:
                        continue
                    self.emit({'jsonrpc':'2.0','method':'notifications/claude/channel','params':{
                        'content':row['body'],'meta':{'project_id':binding['project_id'],
                        'root':binding['root'],'recipient':binding['name'],'sender':row['sender'],
                        'message_id':str(row['id']),'kind':row['kind']}}})
                    self.seen.add(row['id'])
            except sqlite3.OperationalError:
                continue
            except (OSError,ValueError,KeyError):
                # Binding errors must not turn into a notification loop or cross-project fallback.
                self.stop.set()
    def close(self):
        self.stop.set();self.thread.join(timeout=2)
