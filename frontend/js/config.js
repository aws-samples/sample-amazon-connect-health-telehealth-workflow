// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Connect Health EHR Interface Configuration
 * 
 * Update these values when deploying to different environments.
 * See DEPLOYMENT_GUIDE.md Step 3b for instructions.
 */

(function() {
    'use strict';
    
    // Detect environment based on hostname
    const hostname = window.location.hostname;
    
    // ==========================================================================
    // DEMO MODE
    // ==========================================================================
    // Demo mode. On localhost there is usually no AWS account wired up, so
    // default it ON: the README advertises a no-AWS local demo and without
    // this the schedule renders empty on first run (the live HealthLake call
    // fails). An explicit choice stored in localStorage always wins, so
    // toggling it off on localhost is respected.
    var _demoPref = localStorage.getItem('demoMode');
    var _isLocal = ['localhost', '127.0.0.1', '::1', ''].indexOf(hostname) !== -1;
    window.DEMO_MODE = _demoPref === null ? _isLocal : _demoPref === 'true';
    
    window.demoHeaders = function(extra) {
        const h = extra ? Object.assign({}, extra) : {};
        if (window.DEMO_MODE) h['X-Demo-Mode'] = 'true';
        return h;
    };
    
    window.toggleDemoMode = function() {
        window.DEMO_MODE = !window.DEMO_MODE;
        localStorage.setItem('demoMode', window.DEMO_MODE);
        _updateDemoBadge();
        console.log('[Demo] Mode:', window.DEMO_MODE ? 'ON' : 'OFF');
        // The patient list and other page data are fetched once on page load,
        // so the X-Demo-Mode header only affects LATER requests. Without a
        // reload the schedule stays empty after enabling demo mode (the initial
        // live call already failed). Reload so everything re-fetches in the
        // newly selected mode.
        window.location.reload();
    };
    
    function _updateDemoBadge() {
        let badge = document.getElementById('demoBadge');
        if (window.DEMO_MODE) {
            if (!badge) {
                badge = document.createElement('div');
                badge.id = 'demoBadge';
                badge.style.cssText = 'position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;' +
                    'background:#f59e0b;color:#000;padding:3px 12px;border-radius:12px;font-size:11px;' +
                    'font-weight:600;letter-spacing:0.5px;cursor:pointer;user-select:none;opacity:0.9;' +
                    'font-family:-apple-system,BlinkMacSystemFont,sans-serif;box-shadow:0 1px 4px rgba(0,0,0,0.2);';
                badge.textContent = 'DEMO MODE';
                badge.title = 'Click or Ctrl+Shift+D to toggle off';
                badge.onclick = window.toggleDemoMode;
                document.body.appendChild(badge);
            }
            badge.style.display = 'block';
        } else if (badge) {
            badge.style.display = 'none';
        }
    }
    
    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && e.shiftKey && e.key === 'D') {
            e.preventDefault();
            window.toggleDemoMode();
        }
    });
    
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _updateDemoBadge);
    } else {
        _updateDemoBadge();
    }
    
    // ==========================================================================
    // COGNITO AUTHENTICATION
    // ==========================================================================
    // Set these after deploying the cognito-stack.yaml CloudFormation template.
    // Leave empty to disable authentication (local development default).
    window.COGNITO_CONFIG = {
        userPoolId: 'YOUR_COGNITO_USER_POOL_ID',
        clientId: 'YOUR_COGNITO_CLIENT_ID',
        region: 'us-east-1'
    };

    // ==========================================================================
    // CLINIC CONTACT NUMBERS
    // ==========================================================================
    // Replace these with your actual clinic phone numbers.
    // SCHEDULING_PHONE appears in the SMS follow-up message template.
    // OFFICE_PHONE appears in the UI footer and SMS message.
    window.CLINIC_PHONE = {
        schedulingNumber: '(555) 123-4567',   // toll-free or scheduling line
        officeNumber: '(555) 123-4567'        // front desk / office line
    };

    // Environment configurations
    // Update the 'deployed' block with your CloudFront distribution URLs after deployment.
    const configs = {
        local: {
            WS_URL: 'ws://localhost:8081/stream',
            BACKEND_URL: 'http://localhost:5000',
            ENV_NAME: 'local'
        },
        deployed: {
            WS_URL: 'wss://ch-bridge-dev-421355715.us-east-1.elb.amazonaws.com/stream',
            BACKEND_URL: 'https://d1exampleabcdef.cloudfront.net',
            ENV_NAME: 'deployed'
        }
    };
    
    let activeConfig;
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
        activeConfig = configs.local;
    } else {
        activeConfig = configs.deployed;
    }
    
    window.WS_URL = activeConfig.WS_URL;
    window.BACKEND_URL = activeConfig.BACKEND_URL;
    window.ENV_NAME = activeConfig.ENV_NAME;
    
    console.log('[Config] Environment:', activeConfig.ENV_NAME);
    console.log('[Config] WebSocket URL:', activeConfig.WS_URL);
    console.log('[Config] Backend URL:', activeConfig.BACKEND_URL);
})();
