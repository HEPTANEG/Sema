#!/usr/bin/env node
import React from 'react';
import {render} from '@hermes/ink';
import {SemaRavenApp} from './SemaRavenApp.js';

process.stdin.setEncoding('utf8');
process.stdout.setDefaultEncoding('utf8');
process.stderr.setDefaultEncoding('utf8');

render(<SemaRavenApp />, {exitOnCtrlC: false});
