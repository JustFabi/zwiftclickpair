<?php

use App\Http\Controllers\BridgeController;
use Illuminate\Support\Facades\Route;

Route::get('/', fn () => view('dashboard'));

// Control panel -> ClickBridge proxy (ClickBridge itself binds 127.0.0.1 only).
Route::get('/api/status', [BridgeController::class, 'status']);
Route::post('/api/pairing', [BridgeController::class, 'pairing']);
Route::post('/api/disconnect', [BridgeController::class, 'disconnect']);
