<?php

namespace App\Http\Controllers;

use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\PendingRequest;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;

/**
 * Thin, authenticated-by-loopback proxy between the dashboard and the
 * ClickBridge control API (Python, http://127.0.0.1:38398).
 *
 * The browser never talks to the bridge directly: same-origin JSON routes
 * here, so CSRF applies (meta tag + headers from the Blade view) and the
 * bridge stays bound to 127.0.0.1.
 */
class BridgeController extends Controller
{
    private function bridge(): PendingRequest
    {
        return Http::baseUrl(config('services.bridge.url'))
            ->timeout(3)
            ->acceptJson();
    }

    /** GET /api/status — always 200; {"online": false} when bridge is down. */
    public function status(): JsonResponse
    {
        try {
            $res = $this->bridge()->get('/api/status');
        } catch (ConnectionException) {
            return response()->json($this->offline());
        }

        if (! $res->successful()) {
            return response()->json($this->offline());
        }

        return response()->json(['online' => true] + $res->json());
    }

    /** POST /api/pairing {"enabled": bool} — pause/resume automatic pairing. */
    public function pairing(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'enabled' => ['required', 'boolean'],
        ]);

        try {
            $res = $this->bridge()->post('/api/pairing', $validated);
        } catch (ConnectionException) {
            return response()->json(['ok' => false, 'error' => 'ClickBridge is not running'], 503);
        }

        return response()->json(
            $res->json() ?: ['ok' => true],
            $res->successful() ? 200 : 502,
        );
    }

    /** POST /api/disconnect — drop the Click session and all app sessions. */
    public function disconnect(): JsonResponse
    {
        try {
            $res = $this->bridge()->post('/api/disconnect');
        } catch (ConnectionException) {
            return response()->json(['ok' => false, 'error' => 'ClickBridge is not running'], 503);
        }

        return response()->json(
            $res->json() ?: ['ok' => true],
            $res->successful() ? 200 : 502,
        );
    }

    private function offline(): array
    {
        return [
            'online' => false,
            'error' => 'ClickBridge is not reachable on '.config('services.bridge.url'),
        ];
    }
}
