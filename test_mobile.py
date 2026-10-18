"""Mobile/desktop integration regression tests (isolated in-memory simulation)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ['KAKAO_REST_KEY'] = ''
os.environ['OPENAI_API_KEY'] = ''
os.environ['GEMINI_API_KEY'] = ''
_tmp = tempfile.TemporaryDirectory()
os.environ['DB_PATH'] = str(Path(_tmp.name) / 'test.db')

from fastapi.testclient import TestClient
from app.main import app
from app.rider import rider
from app import auth, simulation
from app.simulation import fleet, KST


class MobileIntegration(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        auth.init_db()
        rider.sessions.clear(); rider.reservations.clear(); rider.receipts.clear()
        for v in fleet.vehicles.values():
            v.status = simulation.STATUS_WAITING
            v.battery = 90; v.incident = None; v.repositioning = False
            v.await_board = False; v.cruising = False; v.passenger = None
            v.via_final = None; v.path = None
        self.sid = self.id()
        self.body = dict(session=self.sid, origin='한국공학대학교', destination='정왕역',
                         via='성담스퀘어', passengers=2, payment_method='토스페이', request='캐리어 2개')

    def post(self, path, body):
        r = self.client.post('/api/' + path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def login(self):
        r = self.client.post('/login', data=dict(email='admin@robotaxi.local', password='admin1234'))
        self.assertEqual(r.status_code, 200)

    def test_mobile_page_and_places_match_desktop(self):
        self.login()
        self.assertEqual(self.client.get('/rider').status_code, 200)
        self.assertEqual(self.client.get('/api/places').json()['places'],
                         self.client.get('/api/rider/places').json()['places'])
        desktop = self.client.get('/api/state').json()['vehicles']
        mobile = self.client.get('/api/rider/vehicles').json()['vehicles']
        self.assertEqual({v['id']:(v['lat'], v['lng']) for v in desktop},
                         {v['id']:(v['lat'], v['lng']) for v in mobile})

    def test_booking_metadata_and_desktop_trip(self):
        self.assertTrue(self.post('rider/book', self.body)['ok'])
        s = rider.sessions[self.sid]
        tr = self.client.get('/api/rider/track', params={'session': self.sid}).json()
        self.assertEqual(tr['payment_method'], '토스페이')
        self.assertEqual(tr['via']['name'], '성담스퀘어')
        self.assertEqual(tr['request'], '캐리어 2개')
        t = next(t for t in fleet.trips_log if t['seq'] == s['trip_seq'])
        self.assertEqual((t['origin'],t['destination']), ('한국공학대학교','정왕역'))
        self.assertEqual(t['fare'], s['fare'])
        self.login()
        result = self.client.get('/api/trips')
        self.assertEqual(result.status_code, 200)
        rows = result.json()['rows']
        self.assertTrue(any(t['seq'] == s['trip_seq'] for t in rows))

    def test_duplicate_booking_keeps_original_vehicle(self):
        self.assertTrue(self.post('rider/book', self.body)['ok'])
        vehicle = rider.sessions[self.sid]['vehicle_id']
        self.assertFalse(self.post('rider/book', self.body)['ok'])
        self.assertEqual(rider.sessions[self.sid]['vehicle_id'], vehicle)

    def test_via_continues_without_client_poll_and_counts_once(self):
        self.post('rider/book', self.body)
        s = rider.sessions[self.sid]; v = fleet.vehicles[s['vehicle_id']]
        v.lat, v.lng = s['pickup']['lat'], s['pickup']['lng']
        self.assertTrue(self.post('rider/board', {'session': self.sid})['ok'])
        trips = fleet.session_trips
        v.lat, v.lng = v.dest_lat, v.dest_lng
        old = simulation.ACCIDENT_PROB_PER_TICK; simulation.ACCIDENT_PROB_PER_TICK = 0
        try:
            fleet._tick_driving(v)
            self.assertEqual(v.dest_name, '정왕역')
            self.assertEqual(v.status, simulation.STATUS_DRIVING)
            self.assertEqual(fleet.session_trips, trips)
            v.lat, v.lng = v.dest_lat, v.dest_lng; v.path = None
            fleet._tick_driving(v)
            self.assertEqual(fleet.session_trips, trips+1)
        finally:
            simulation.ACCIDENT_PROB_PER_TICK = old

    def test_boarded_state_and_receipt_survive_refresh(self):
        self.post('rider/book', self.body)
        s = rider.sessions[self.sid]; v = fleet.vehicles[s['vehicle_id']]
        v.lat, v.lng = s['pickup']['lat'], s['pickup']['lng']
        self.post('rider/board', {'session':self.sid})
        tr = self.client.get('/api/rider/track', params={'session':self.sid}).json()
        self.assertTrue(tr['boarded']); self.assertEqual(tr['passengers'],2)
        rc = self.post('rider/complete', {'session':self.sid})
        self.assertEqual(rc['payment_method'],'토스페이')
        state = self.client.get('/api/rider/state', params={'session':self.sid}).json()
        self.assertEqual(state['trips'][0],rc)

    def test_reservation_saved_and_cancelled(self):
        b = dict(self.body, when=(datetime.now(KST)+timedelta(hours=1)).isoformat())
        self.assertTrue(self.post('rider/reserve',b)['ok'])
        self.assertEqual(rider.state(self.sid)['reservation']['status'],'pending')
        self.assertFalse(self.post('rider/reserve',b)['ok'])
        self.assertTrue(self.post('rider/reservation/cancel',{'session':self.sid})['ok'])
        self.assertEqual(rider.state(self.sid)['reservation']['status'],'cancelled')

    def test_due_reservation_dispatches_real_fleet(self):
        b = dict(self.body, when=(datetime.now(KST)+timedelta(hours=1)).isoformat())
        self.post('rider/reserve',b)
        rider.reservations[self.sid]['when']=(datetime.now(KST)-timedelta(seconds=1)).isoformat()
        rider.process_reservations()
        self.assertEqual(rider.reservations[self.sid]['status'],'dispatched')
        self.assertTrue(rider.track(self.sid)['active'])
        self.assertEqual(rider.track(self.sid)['payment_method'],'토스페이')

    def test_invalid_or_past_reservation_rejected(self):
        for when in ['not-a-time',(datetime.now(KST)-timedelta(hours=1)).isoformat()]:
            self.assertFalse(self.post('rider/reserve',dict(self.body,when=when))['ok'])

    def test_cancel_updates_desktop_trip(self):
        self.post('rider/book',self.body)
        seq=rider.sessions[self.sid]['trip_seq']
        self.post('rider/cancel',{'session':self.sid})
        t=next(t for t in fleet.trips_log if t['seq']==seq)
        self.assertEqual(t['status'],'취소');self.assertEqual(t['fare'],0)
        self.assertFalse(rider.track(self.sid)['active'])

    def test_suv_assignment(self):
        self.assertTrue(self.post('rider/book',dict(self.body,passengers=6))['ok'])
        self.assertEqual(rider.track(self.sid)['vehicle']['car_type'],'SUV')

    def test_route_without_rest_key_reports_unavailable(self):
        self.post('rider/book',self.body)
        self.assertFalse(self.post('rider/route',{'session':self.sid,'kind':'free'})['ok'])

    def test_support_roundtrip_with_admin(self):
        self.post('support/request',{'session':self.sid})
        self.post('support/send',{'session':self.sid,'text':'모바일에서 문의합니다'})
        self.login()
        history=self.client.get('/api/support/history',params={'session':self.sid}).json()
        self.assertEqual(history['messages'][-1]['text'],'모바일에서 문의합니다')
        self.post('support/reply',{'session':self.sid,'text':'관제에서 확인했습니다'})
        poll=self.client.get('/api/support/poll',params={'session':self.sid}).json()
        self.assertTrue(any(m['text']=='관제에서 확인했습니다' for m in poll['messages']))
        self.post('support/end',{'session':self.sid})
        self.assertFalse(self.client.get('/api/support/poll',params={'session':self.sid}).json()['active'])


if __name__ == '__main__':
    unittest.main()
