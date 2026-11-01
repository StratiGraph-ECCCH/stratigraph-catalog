# reader-dist/

Qui il workflow `immagine.yml` mette la `dist/` del reader di EMStudio
(`npm run build:reader`, dal tag pinnato in `EMSTUDIO_REF`) prima di costruire
l'immagine; il `Dockerfile` la copia in `/srv/em-reader-dist`.

In un checkout c'è solo questo file: un `docker build` locale produce
un'immagine senza reader (`/study/{id}/narrative` → 501), e il dev-stack monta
comunque la `dist` del checkout di EMStudio sopra questa copia.
