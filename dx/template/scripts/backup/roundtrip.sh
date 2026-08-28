./scripts/backup/backup.sh
docker-compose down -v
docker-compose up -d 
sleep 3
./scripts/backup/restore.sh
 