#!/usr/bin/env python3
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'log_analyzer.settings')
django.setup()

from core.models import Server, ServerHeartbeat
from core.views import _calculate_server_status

print('=== Server Status Summary ===\n')
for server in Server.objects.all():
    hb = ServerHeartbeat.objects.filter(server=server).first()
    status = _calculate_server_status(server)
    hb_time = hb.last_heartbeat if hb else "None"
    print(f'{server.name} (ID: {server.id}): {status}')
    print(f'  Heartbeat: {hb_time}')
    print()

