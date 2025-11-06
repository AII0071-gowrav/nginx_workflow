    const express=require('express');const cors=require('cors');const app=express();app.use(cors());
    app.get('/api/message',(r,s)=>s.json({message:'hi'}));app.listen(5000);
